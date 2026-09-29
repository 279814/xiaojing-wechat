from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from click.testing import CliRunner

from .settings import AppSettings
from .wechat_cli_bundle import bundled_wechat_cli_path, ensure_wechat_cli_import_path


@dataclass
class WechatCliStatus:
    available: bool
    initialized: bool
    message: str
    config_file: str = ""
    keys_file: str = ""
    db_dir: str = ""
    bundled_source: str = ""
    # Full scanner / init log (key material redacted), kept separate from the short
    # user-facing message so the UI can show it behind a details view.
    log: str = ""
    # True when an init/reset attempt failed, even if the previous keys were restored
    # and the connection is still usable (initialized=True).
    init_failed: bool = False
    # db_storage of the account WeChat is currently running, when it differs from db_dir.
    active_db_dir: str = ""


_HEX_KEY_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64,}(?![0-9a-fA-F])")


def redact_key_material(text: str) -> str:
    """Strip encryption keys from scanner output before it can reach the UI."""
    lines = []
    for line in (text or "").splitlines():
        if "enc_key" in line:
            line = re.sub(r"enc_key\s*[=:]\s*\S+", "enc_key=<redacted>", line)
        lines.append(_HEX_KEY_RE.sub("<redacted>", line))
    return "\n".join(lines)


def summarize_init_failure(output: str, exit_code: int) -> str:
    """Turn a raw wechat-cli init log into one short Chinese sentence.

    The raw scanner log can be thousands of lines; never surface it as the primary
    message. Callers keep the full log in ``WechatCliStatus.log``.
    """
    text = output or ""
    if "Weixin.exe 未运行" in text:
        return "微信未运行：请先打开并登录 Windows 微信后重试。"
    if "未检测到微信数据目录" in text or "未能自动检测到微信数据目录" in text:
        return "未检测到微信数据目录：请确认 Windows 微信已登录后重试。"

    ratio = ""
    match = re.search(r"结果:\s*(\d+)\s*/\s*(\d+)\s*salts", text)
    if match:
        found, total = match.group(1), match.group(2)
        ratio = f"（{found}/{total}）"
        if found == "0":
            return f"重置连接失败：未能从微信进程提取到密钥{ratio}。请确认微信已登录，然后重试。"
    if "未能从任何微信进程中提取到密钥" in text or "未提取到任何密钥" in text:
        return f"重置连接失败：未能从微信进程提取到密钥{ratio}。请确认微信已登录，然后重试。"
    return f"重置连接失败{ratio}（exit_code={exit_code}）。详情见日志。"


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a or "")) == os.path.normcase(os.path.normpath(b or ""))


def account_id(db_dir: str) -> str:
    """wxid of the account owning ``<xwechat_files>/<wxid>_<hex>/db_storage``."""
    base = os.path.basename(os.path.dirname(os.path.normpath(db_dir or "")))
    match = re.fullmatch(r"(.+)_[0-9a-fA-F]{4,}", base)
    return match.group(1) if match else base


def _wechat_data_roots() -> list[str]:
    """Data roots Windows WeChat 4 records under %APPDATA%\\Tencent\\xwechat\\config."""
    config_dir = os.path.join(os.environ.get("APPDATA", ""), "Tencent", "xwechat", "config")
    roots: list[str] = []
    if not os.path.isdir(config_dir):
        return roots
    for ini_file in Path(config_dir).glob("*.ini"):
        for enc in ("utf-8", "gbk"):
            try:
                content = ini_file.read_text(encoding=enc)[:1024].strip()
                break
            except (UnicodeDecodeError, OSError):
                content = ""
        if content and not any(c in content for c in "\n\r\x00") and os.path.isdir(content):
            roots.append(os.path.join(content, "xwechat_files"))
    return roots


def _activity_mtime(db_dir: str) -> float:
    latest = 0.0
    for rel in ("session/session.db", "session/session.db-wal", "message/message_0.db-wal"):
        try:
            latest = max(latest, os.path.getmtime(os.path.join(db_dir, rel)))
        except OSError:
            continue
    return latest


def active_db_dir(configured_db_dir: str = "") -> str:
    """db_storage of the account WeChat is currently writing to ("" if none found).

    One PC can hold several logged-in-before accounts side by side; the running
    account is the one whose session database was written most recently.
    """
    xwechat_roots = list(_wechat_data_roots())
    if configured_db_dir:
        xwechat_roots.append(os.path.dirname(os.path.dirname(os.path.normpath(configured_db_dir))))
    seen: set[str] = set()
    candidates: list[str] = []
    for root in xwechat_roots:
        for match in Path(root).glob("*/db_storage"):
            key = os.path.normcase(os.path.normpath(str(match)))
            if key not in seen and (match / "session" / "session.db").is_file():
                seen.add(key)
                candidates.append(str(match))
    if not candidates:
        return ""
    return max(candidates, key=_activity_mtime)


def verify_saved_keys(keys_file: Path, db_dir: str) -> tuple[int, int]:
    """(verified, checked): how many saved keys still open their db in ``db_dir``.

    Uses the SQLCipher page-1 HMAC only; no memory scan and no key output.
    """
    from wechat_cli.core.key_utils import strip_key_metadata
    from wechat_cli.keys.common import PAGE_SZ, verify_enc_key

    try:
        keys = strip_key_metadata(json.loads(keys_file.read_text(encoding="utf-8")))
    except Exception:
        return 0, 0
    verified = checked = 0
    for rel, info in keys.items():
        path = os.path.join(db_dir, rel.replace("\\", os.sep).replace("/", os.sep))
        try:
            with open(path, "rb") as fh:
                page1 = fh.read(PAGE_SZ)
            enc_key = bytes.fromhex(str(info.get("enc_key") or ""))
        except (OSError, ValueError, AttributeError):
            continue
        if len(page1) < PAGE_SZ or len(enc_key) != 32:
            continue
        checked += 1
        if verify_enc_key(enc_key, page1):
            verified += 1
    return verified, checked


class WechatCliManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def _paths(self) -> tuple[Path, Path]:
        from wechat_cli.core.config import CONFIG_FILE, KEYS_FILE

        config_file = Path(self.settings.wechat_cli_config or CONFIG_FILE)
        keys_file = Path(
            KEYS_FILE if not self.settings.wechat_cli_config else config_file.parent / "all_keys.json"
        )
        return config_file, keys_file

    def status(self) -> WechatCliStatus:
        bundled = bundled_wechat_cli_path()
        ensure_wechat_cli_import_path()
        try:
            from wechat_cli.core.config import auto_detect_db_dir
        except Exception as exc:
            return WechatCliStatus(
                available=False,
                initialized=False,
                message=f"内置 wechat-cli 不可用: {exc}",
                bundled_source=str(bundled),
            )

        config_file, keys_file = self._paths()
        db_dir = ""
        if config_file.exists():
            try:
                payload = json.loads(config_file.read_text(encoding="utf-8"))
                db_dir = str(payload.get("db_dir") or "")
            except Exception:
                db_dir = ""
        initialized = config_file.exists() and self._keys_present(keys_file) and bool(db_dir)
        if initialized:
            status = WechatCliStatus(
                available=True,
                initialized=True,
                message="微信连接已初始化",
                config_file=str(config_file),
                keys_file=str(keys_file),
                db_dir=db_dir,
                bundled_source=str(bundled),
            )
            active = active_db_dir(db_dir)
            if active and not _same_path(active, db_dir):
                status.active_db_dir = active
                status.message = (
                    f"微信当前登录的账号 {account_id(active)} 与已连接的账号 {account_id(db_dir)} 不一致，"
                    "请点击「重置连接」。"
                )
            return status

        try:
            detected = auto_detect_db_dir()
        except Exception:
            detected = None
        hint = f"检测到数据目录: {detected}" if detected else "未检测到微信数据目录，请确认 Windows 微信已登录"
        return WechatCliStatus(
            available=True,
            initialized=False,
            message=f"微信连接未初始化。{hint}",
            config_file=str(config_file),
            keys_file=str(keys_file),
            db_dir=str(detected or ""),
            bundled_source=str(bundled),
        )

    @staticmethod
    def _keys_present(keys_file: Path) -> bool:
        """True only when the keys file exists and holds at least one key entry.

        A previously-good all_keys.json must never be considered "initialized" if a
        failed rescan left it empty.
        """
        if not keys_file.exists():
            return False
        try:
            payload = json.loads(keys_file.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        return any(not str(k).startswith("_") for k in payload)

    @staticmethod
    def _write_db_dir(config_file: Path, db_dir: str) -> None:
        try:
            payload = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["db_dir"] = db_dir
        config_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _key_count_bytes(raw: bytes | None) -> int:
        if not raw:
            return 0
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return 0
        if not isinstance(payload, dict):
            return 0
        return sum(1 for k in payload if not str(k).startswith("_"))

    @classmethod
    def _key_count(cls, keys_file: Path) -> int:
        try:
            return cls._key_count_bytes(keys_file.read_bytes())
        except OSError:
            return 0

    def initialize(self, db_dir: str = "", force: bool = False) -> WechatCliStatus:
        ensure_wechat_cli_import_path()
        try:
            from wechat_cli.main import cli
        except Exception as exc:
            return WechatCliStatus(False, False, f"内置 wechat-cli 不可用: {exc}")

        config_file, keys_file = self._paths()
        # Preserve a previously working key set: a forced rescan that matches 0 (or
        # fewer valid) keys must not degrade what already works.
        backup: bytes | None = None
        before = self.status()
        if force and self._keys_present(keys_file):
            try:
                backup = keys_file.read_bytes()
            except OSError:
                backup = None

        # Scan for the running account's salts: wechat-cli's own auto-detect takes the
        # first account folder alphabetically, which after an account switch is the
        # old account and makes the scan match 0 keys.
        target = db_dir.strip() or before.active_db_dir or before.db_dir
        if force and backup is not None and target:
            verified, checked = verify_saved_keys(keys_file, target)
            if checked and verified == checked:
                if not _same_path(target, before.db_dir):
                    self._write_db_dir(config_file, target)
                status = self.status()
                status.message = f"已保存的微信连接仍然有效（{verified}/{checked} 个数据库可解密），无需重新提取密钥。"
                return status

        args = ["init"]
        if target:
            args.extend(["--db-dir", target])
        if force:
            args.append("--force")
        result = CliRunner().invoke(cli, args)
        output = redact_key_material((result.output or "").strip())

        next_status = self.status()
        if result.exit_code == 0 and next_status.initialized:
            old_count = self._key_count_bytes(backup)
            new_count = self._key_count(keys_file)
            same_dir = bool(before.db_dir) and _same_path(before.db_dir, next_status.db_dir)
            if backup is not None and same_dir and new_count < old_count:
                try:
                    keys_file.write_bytes(backup)
                except OSError:
                    pass
                restored_status = self.status()
                restored_status.message = (
                    f"重置连接未完成：本次只找到 {new_count}/{old_count} 个密钥，已保留上次可用的连接。"
                )
                restored_status.log = output
                restored_status.init_failed = True
                return restored_status
            next_status.message = "微信连接初始化完成"
            next_status.log = output
            return next_status

        # Failure: restore the backup so the app keeps working with the old keys.
        restored = False
        if backup is not None and not next_status.initialized:
            try:
                keys_file.write_bytes(backup)
                restored = True
            except OSError:
                restored = False
            next_status = self.status()

        summary = summarize_init_failure(output, result.exit_code)
        if (restored or backup is not None) and next_status.initialized:
            if target and not _same_path(target, next_status.db_dir):
                summary += f" 仍在读取旧账号 {account_id(next_status.db_dir)} 的数据。"
            else:
                summary += " 已保留上次可用的连接，仍可继续使用。"
        return WechatCliStatus(
            available=next_status.available,
            initialized=next_status.initialized,
            message=summary,
            config_file=next_status.config_file,
            keys_file=next_status.keys_file,
            db_dir=next_status.db_dir,
            bundled_source=next_status.bundled_source,
            log=output,
            init_failed=True,
        )

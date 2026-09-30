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
    # True when this init/reset moved the connection to a different WeChat account.
    account_switched: bool = False


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


# WeChat keeps the -wal files after logout and only writes session.db when messages
# sync, so an account that just logged in may have the older session.db. The -shm
# files are rewritten when WeChat opens the databases at login.
_ACTIVITY_FILES = (
    "session/session.db",
    "session/session.db-wal",
    "session/session.db-shm",
    "message/message_0.db-wal",
    "message/message_0.db-shm",
    "contact/contact.db-wal",
    "contact/contact.db-shm",
)


def _activity_mtime(db_dir: str) -> float:
    latest = 0.0
    for rel in _ACTIVITY_FILES:
        try:
            latest = max(latest, os.path.getmtime(os.path.join(db_dir, rel)))
        except OSError:
            continue
    return latest


def _file_open_by_other_process(path: str) -> bool | None:
    """Whether another process holds ``path`` open (None when it cannot be told).

    Uses the Windows Restart Manager, which only lists handle owners: it neither
    opens the file nor touches the WeChat process.
    """
    if os.name != "nt" or not os.path.isfile(path):
        return None
    import ctypes
    from ctypes import wintypes

    class _UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", wintypes.DWORD), ("start_time", wintypes.FILETIME)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", _UniqueProcess),
            ("app_name", wintypes.WCHAR * 256),
            ("service_name", wintypes.WCHAR * 64),
            ("app_type", ctypes.c_int),
            ("app_status", wintypes.DWORD),
            ("session_id", wintypes.DWORD),
            ("restartable", wintypes.BOOL),
        ]

    try:
        rm = ctypes.WinDLL("rstrtmgr")
    except OSError:
        return None
    session = wintypes.DWORD()
    session_key = ctypes.create_unicode_buffer(64)
    if rm.RmStartSession(ctypes.byref(session), 0, session_key) != 0:
        return None
    try:
        files = (wintypes.LPCWSTR * 1)(path)
        if rm.RmRegisterResources(session, 1, files, 0, None, 0, None) != 0:
            return None
        needed, count, reasons = wintypes.UINT(0), wintypes.UINT(0), wintypes.DWORD()
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if rc == 0 and needed.value == 0:
            return False
        infos = (_ProcessInfo * max(needed.value, 1))()
        count = wintypes.UINT(len(infos))
        rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos, ctypes.byref(reasons))
        if rc != 0:
            return None
        return any(infos[i].process.pid != os.getpid() for i in range(count.value))
    finally:
        rm.RmEndSession(session)


_OPEN_CHECK_FILES = ("session/session.db", "contact/contact.db", "message/message_0.db")


def _open_by_wechat(db_dir: str) -> bool | None:
    """True if WeChat holds any of the account's main databases open."""
    answers = [_file_open_by_other_process(os.path.join(db_dir, rel)) for rel in _OPEN_CHECK_FILES]
    if any(answers):
        return True
    return False if any(a is False for a in answers) else None


def active_db_dir(configured_db_dir: str = "") -> str:
    """db_storage of the account WeChat is currently logged in to ("" if none found).

    One PC can hold several logged-in-before accounts side by side. The running
    account is the one whose databases WeChat holds open; when that cannot be told
    (WeChat closed, no Restart Manager), the one with the newest database activity.
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
    in_use = [c for c in candidates if _open_by_wechat(c)]
    return max(in_use or candidates, key=_activity_mtime)


def reset_wechat_cli_caches() -> None:
    """Drop wechat-cli's per-process contact cache.

    The client runs wechat-cli in-process, and ``wechat_cli.core.contacts`` keeps the
    first contact list it loaded for the life of the process, so without this the
    customer list stays on the previous account (and misses renamed contacts).
    """
    ensure_wechat_cli_import_path()
    try:
        from wechat_cli.core import contacts
    except Exception:
        return
    contacts._contact_names = None
    contacts._contact_full = None
    contacts._self_username = None


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
                    "请点击「重置连接」切换到当前账号。"
                )
            return status

        # wechat-cli's own auto-detect takes the first account folder alphabetically.
        detected = active_db_dir()
        if not detected:
            try:
                detected = auto_detect_db_dir() or ""
            except Exception:
                detected = ""
        hint = (
            f"检测到当前微信账号 {account_id(detected)}，请点击「重置连接」。"
            if detected
            else "未检测到微信数据目录，请确认 Windows 微信已登录后点击「重置连接」。"
        )
        return WechatCliStatus(
            available=True,
            initialized=False,
            message=f"微信连接未初始化：{hint}",
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

    def reset_connection(self, db_dir: str = "") -> WechatCliStatus:
        """Connect to the WeChat account that is logged in now (「重置连接」).

        This replaces the former 「初始化微信连接」 button, which ran plain
        ``wechat-cli init``: that command returns at once when config.json and
        all_keys.json already exist, so it could never move to another account.
        """
        ensure_wechat_cli_import_path()
        try:
            from wechat_cli.main import cli
        except Exception as exc:
            return WechatCliStatus(False, False, f"内置 wechat-cli 不可用: {exc}")

        config_file, keys_file = self._paths()
        # Preserve a previously working key set: a rescan that matches 0 (or fewer
        # valid) keys, or fails for a newly logged-in account, must not degrade what
        # already works.
        backup: bytes | None = None
        before = self.status()
        saved_db_dir = before.db_dir if before.initialized else ""
        if self._keys_present(keys_file):
            try:
                backup = keys_file.read_bytes()
            except OSError:
                backup = None

        # Target the account WeChat is logged in to now: wechat-cli's own auto-detect
        # takes the first account folder alphabetically, which after an account switch
        # is the old account and makes the scan match 0 keys.
        target = db_dir.strip() or before.active_db_dir or before.db_dir
        switching = bool(saved_db_dir) and bool(target) and not _same_path(target, saved_db_dir)
        if backup is not None and target:
            verified, checked = verify_saved_keys(keys_file, target)
            if checked and verified == checked:
                if not _same_path(target, saved_db_dir):
                    self._write_db_dir(config_file, target)
                reset_wechat_cli_caches()
                status = self.status()
                status.account_switched = switching
                if switching:
                    status.message = (
                        f"已切换到当前微信账号 {account_id(target)}（{verified}/{checked} 个数据库可解密）。"
                    )
                else:
                    status.message = (
                        f"当前微信账号 {account_id(target)} 已连接（{verified}/{checked} 个数据库可解密），"
                        "无需重新提取密钥。"
                    )
                return status

        if backup is not None:
            self._write_keys_backup(keys_file, backup, saved_db_dir)
        # Without --force `wechat-cli init` exits 0 as soon as a config exists, so a
        # needed rescan (other account, stale keys) would silently do nothing.
        args = ["init", "--force"]
        if target:
            args.extend(["--db-dir", target])
        result = CliRunner().invoke(cli, args)
        output = redact_key_material((result.output or "").strip())

        next_status = self.status()
        if result.exit_code == 0 and next_status.initialized:
            old_count = self._key_count_bytes(backup)
            new_count = self._key_count(keys_file)
            same_dir = bool(saved_db_dir) and _same_path(saved_db_dir, next_status.db_dir)
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
            reset_wechat_cli_caches()
            switched = bool(saved_db_dir) and not same_dir
            next_status.account_switched = switched
            verb = "已切换到" if switched else "已连接"
            next_status.message = (
                f"{verb}当前微信账号 {account_id(next_status.db_dir)}（提取到 {new_count} 个数据库密钥）。"
            )
            next_status.log = output
            return next_status

        # Failure: the scanner only writes keys on success, but make sure the file and
        # the configured account are exactly what worked before.
        if backup is not None:
            try:
                if keys_file.read_bytes() != backup:
                    keys_file.write_bytes(backup)
            except OSError:
                try:
                    keys_file.write_bytes(backup)
                except OSError:
                    pass
            if saved_db_dir and not _same_path(next_status.db_dir, saved_db_dir):
                self._write_db_dir(config_file, saved_db_dir)
            next_status = self.status()

        summary = summarize_init_failure(output, result.exit_code)
        if backup is not None and next_status.initialized:
            if switching:
                # Lead with the outcome: the sidebar label shows only the first ~120 chars.
                summary = (
                    f"顾客列表没有切换：微信当前登录的是 {account_id(target)}，但没有拿到该账号的密钥；"
                    f"仍在读取旧账号 {account_id(next_status.db_dir)} 的数据。{summary}"
                )
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
            active_db_dir=target if switching else "",
        )

    @staticmethod
    def _write_keys_backup(keys_file: Path, raw: bytes, db_dir: str) -> None:
        """Copy the working key file aside (per account) before a rescan replaces it."""
        name = account_id(db_dir) if db_dir else "previous"
        try:
            keys_file.with_name(f"all_keys.{name}.bak.json").write_bytes(raw)
        except OSError:
            pass

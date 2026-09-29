from __future__ import annotations

import json
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


class WechatCliManager:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def status(self) -> WechatCliStatus:
        bundled = bundled_wechat_cli_path()
        ensure_wechat_cli_import_path()
        try:
            from wechat_cli.core.config import CONFIG_FILE, KEYS_FILE, auto_detect_db_dir
        except Exception as exc:
            return WechatCliStatus(
                available=False,
                initialized=False,
                message=f"内置 wechat-cli 不可用: {exc}",
                bundled_source=str(bundled),
            )

        config_file = Path(self.settings.wechat_cli_config or CONFIG_FILE)
        keys_file = Path(KEYS_FILE if not self.settings.wechat_cli_config else config_file.parent / "all_keys.json")
        db_dir = ""
        if config_file.exists():
            try:
                payload = json.loads(config_file.read_text(encoding="utf-8"))
                db_dir = str(payload.get("db_dir") or "")
            except Exception:
                db_dir = ""
        initialized = config_file.exists() and keys_file.exists() and bool(db_dir)
        if initialized:
            return WechatCliStatus(
                available=True,
                initialized=True,
                message="微信连接已初始化",
                config_file=str(config_file),
                keys_file=str(keys_file),
                db_dir=db_dir,
                bundled_source=str(bundled),
            )

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

    def initialize(self, db_dir: str = "", force: bool = False) -> WechatCliStatus:
        ensure_wechat_cli_import_path()
        try:
            from wechat_cli.main import cli
        except Exception as exc:
            return WechatCliStatus(False, False, f"内置 wechat-cli 不可用: {exc}")

        args = ["init"]
        if db_dir.strip():
            args.extend(["--db-dir", db_dir.strip()])
        if force:
            args.append("--force")
        result = CliRunner().invoke(cli, args)
        next_status = self.status()
        if result.exit_code == 0 and next_status.initialized:
            next_status.message = "微信连接初始化完成"
            return next_status
        output = (result.output or "").strip()
        err = output or f"wechat-cli init 失败，exit_code={result.exit_code}"
        if "Weixin.exe 未运行" in err:
            err += "。请先打开并登录 Windows 微信。"
        return WechatCliStatus(
            available=next_status.available,
            initialized=False,
            message=err,
            config_file=next_status.config_file,
            keys_file=next_status.keys_file,
            db_dir=next_status.db_dir,
            bundled_source=next_status.bundled_source,
        )


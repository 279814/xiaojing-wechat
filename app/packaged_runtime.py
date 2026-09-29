"""Runtime support that only the packaged Autosale.exe uses."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from .settings import AppSettings, app_data_dir

# Modules the client imports lazily. A missing one only surfaces when the
# salesperson clicks something, so the self-check imports them up front.
_LAZY_MODULES = (
    "app.main",
    "click.testing",
    "wechat_cli.main",
    "wechat_cli.core.config",
    "wechat_cli.core.context",
    "wechat_cli.core.messages",
    "wechat_cli.keys.scanner_windows",
    "zstandard",
    "Crypto.Cipher.AES",
    "pyperclip",
    "app.send.windows",
    "app.send.overlay",
    "winrt.windows.media.ocr",
    "requests",
)


def _exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def _writable_file(name: str) -> Path:
    candidate = _exe_dir() / name
    try:
        with open(candidate, "a", encoding="utf-8"):
            pass
        return candidate
    except OSError:
        fallback = app_data_dir() / name
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


def configure_logging() -> Path:
    """Write logs next to Autosale.exe, or to %APPDATA%\\XiaojingAutosale if that folder is read-only."""
    path = _writable_file("autosale.log")
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    previous_hook = sys.excepthook

    def _log_unhandled(exc_type, exc, tb) -> None:
        logging.getLogger("autosale.client").critical("unhandled exception", exc_info=(exc_type, exc, tb))
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _log_unhandled
    return path


def _check(report: dict[str, Any], name: str, fn) -> Any:
    try:
        value = fn()
        report[name] = {"ok": True, "value": value}
        return value
    except Exception as exc:
        report[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return None


def _zstd_roundtrip() -> str:
    import zstandard

    payload = "小鲸 autosale".encode("utf-8")
    restored = zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(payload))
    if restored != payload:
        raise RuntimeError("zstd roundtrip mismatch")
    return "ok"


def _aes_roundtrip() -> str:
    from Crypto.Cipher import AES

    key, iv, block = b"k" * 32, b"i" * 16, b"0123456789abcdef"
    restored = AES.new(key, AES.MODE_CBC, iv).decrypt(AES.new(key, AES.MODE_CBC, iv).encrypt(block))
    if restored != block:
        raise RuntimeError("AES roundtrip mismatch")
    return "ok"


def _ocr_self_test() -> str:
    """Windows OCR must work, otherwise every send aborts at the chat-title check."""
    from app.send.windows import ocr_bgra

    width, height = 64, 16
    ocr_bgra(width, height, b"\xff" * (width * height * 4))
    return "ok"


def run_dry_run() -> int:
    """Same as ``python -m app.send --dry-run``; the printed lines also go to dry-run.txt next to the exe."""
    from app.send.__main__ import run_dry_run as _run

    return _run(_writable_file("dry-run.txt"))


def run_self_check() -> int:
    """Import every lazily used module and touch WeChat read-only. Returns a process exit code."""
    report: dict[str, Any] = {"python": sys.version.split()[0], "modules": {}, "checks": {}}
    for name in _LAZY_MODULES:
        try:
            importlib.import_module(name)
            report["modules"][name] = "ok"
        except Exception as exc:
            report["modules"][name] = f"{type(exc).__name__}: {exc}"

    checks = report["checks"]
    _check(checks, "zstandard", _zstd_roundtrip)
    _check(checks, "aes", _aes_roundtrip)
    _check(checks, "windows_ocr", _ocr_self_test)

    from .wechat_cli_manager import WechatCliManager
    from .wechat_reader import WechatReader

    settings = AppSettings.load()
    status = _check(checks, "wechat_cli_status", lambda: WechatCliManager(settings).status())
    if status is not None:
        checks["wechat_cli_status"]["value"] = {
            "available": status.available,
            "initialized": status.initialized,
            "message": status.message,
        }
        if not status.available:
            checks["wechat_cli_status"]["ok"] = False
        elif status.initialized:
            reader = WechatReader(settings)
            customers = _check(checks, "customer_contacts", reader.refresh_customer_contacts)
            if customers is not None:
                checks["customer_contacts"]["value"] = len(customers)
                _check(checks, "customer_unread", lambda: len(reader.fetch_customer_unread()))
                if customers:
                    _check(
                        checks,
                        "customer_history",
                        lambda: len(reader.fetch_recent_inbound_messages(customers[0].username, limit=1)),
                    )

    failed = [name for name, value in report["modules"].items() if value != "ok"]
    failed += [name for name, value in checks.items() if not value.get("ok")]
    report["ok"] = not failed
    report["failed"] = failed

    out = _writable_file("self_check.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1

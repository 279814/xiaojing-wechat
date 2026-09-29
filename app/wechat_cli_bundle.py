from __future__ import annotations

import sys
from pathlib import Path


def bundled_wechat_cli_path() -> Path:
    return Path(__file__).resolve().parent / "vendor" / "wechat-cli"


def ensure_wechat_cli_import_path() -> Path | None:
    path = bundled_wechat_cli_path()
    if not path.exists():
        return None
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)
    return path

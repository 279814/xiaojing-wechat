# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# This spec lives in app/. The client package is the parent directory, and
# wechat-cli is vendored as plain source. Both must be importable during
# analysis, otherwise wechat-cli's own imports (zstandard, pycryptodome) are
# never seen and the frozen client cannot read WeChat.
ROOT = Path(SPECPATH).resolve().parent
WECHAT_CLI_SRC = ROOT / "app" / "vendor" / "wechat-cli"
for path in (str(WECHAT_CLI_SRC), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

hiddenimports = []
hiddenimports += collect_submodules("app", filter=lambda name: name != "app.pyi_entry")
hiddenimports += collect_submodules("wechat_cli")
hiddenimports += collect_submodules("Crypto", filter=lambda name: not name.startswith("Crypto.SelfTest"))
hiddenimports += ["zstandard"]
# Windows OCR (chat-title check before sending) is imported lazily.
hiddenimports += collect_submodules("winrt")

datas = []
datas += collect_data_files("wechat_cli")


# Script paths are relative to this spec file.
a = Analysis(
    ["pyi_entry.py"],
    pathex=[str(WECHAT_CLI_SRC), str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["Crypto.SelfTest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Autosale",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Autosale",
)

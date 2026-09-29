"""Calibrate the click layout without sending anything.

    python -m app.send --dry-run

Brings WeChat to the front, shows the overlay, glides the fake cursor over each
target, and OCRs the chat-title region. It never clicks, types or pastes.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "app.send"

from . import WechatSenderError, create_backend


def _dry_run(overlay) -> int:
    backend = create_backend()
    rect = backend.activate()
    scale = backend.scale()
    layout = backend.layout
    print(f"WeChat client rect: {rect}  scale={scale:.2f}")
    problem = layout.check_fits(rect, scale)
    if problem:
        print(f"Layout check: {problem}")
    overlay.begin(rect, backend.coords_are_physical())
    try:
        for name, point in layout.targets(rect, scale).items():
            overlay.set_status(f"试运行：{name}")
            print(f"  {name:14s} -> ({point.x}, {point.y})")
            overlay.move_cursor(point, 500)
            time.sleep(1.0)
            overlay.pulse()
            time.sleep(0.5)
        region = layout.chat_title.resolve(rect, scale)
        overlay.set_cursor_visible(False)
        time.sleep(0.15)
        try:
            readings = list(backend.read_text_candidates(region))
        except WechatSenderError as exc:
            readings = [f"<OCR unavailable: {exc}>"]
        print(f"  chat_title     region {region} OCR passes: {readings!r}")
    finally:
        overlay.end()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", required=True, help="show targets without clicking")
    parser.parse_args()

    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QApplication

    from .overlay import OverlayController

    app = QApplication(sys.argv)
    overlay = OverlayController()
    result = {"code": 1}

    class Worker(QThread):
        def run(self) -> None:
            try:
                result["code"] = _dry_run(overlay)
            except WechatSenderError as exc:
                print(f"Dry run stopped: {exc}")

    worker = Worker()
    worker.finished.connect(lambda: QTimer.singleShot(300, app.quit))
    worker.start()
    app.exec()
    return result["code"]


if __name__ == "__main__":
    raise SystemExit(main())

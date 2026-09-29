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
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "app.send"

from . import WechatSenderError, create_backend


def _dry_run(overlay, emit: Callable[[str], None]) -> int:
    backend = create_backend()
    rect = backend.activate()
    scale = backend.scale()
    layout = backend.layout
    emit(f"WeChat client rect: {rect}  scale={scale:.2f}")
    problem = layout.check_fits(rect, scale)
    if problem:
        emit(f"Layout check: {problem}")
    overlay.begin(rect, backend.coords_are_physical())
    try:
        for name, point in layout.targets(rect, scale).items():
            overlay.set_status(f"试运行：{name}")
            emit(f"  {name:14s} -> ({point.x}, {point.y})")
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
        emit(f"  chat_title     region {region} OCR passes: {readings!r}")
    finally:
        overlay.end()
    return 0


def run_dry_run(report: Path | None = None) -> int:
    """Run the dry run with the overlay. Every printed line is also written to ``report``.

    The packaged exe has no console, so the report file is the only readable output there.
    """
    from PySide6.QtCore import QThread, QTimer
    from PySide6.QtWidgets import QApplication

    from .overlay import OverlayController

    if report is not None:
        report.write_text("", encoding="utf-8")

    def emit(line: str) -> None:
        print(line, flush=True)
        if report is not None:
            with open(report, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    app = QApplication.instance() or QApplication(sys.argv)
    overlay = OverlayController()
    result = {"code": 1}

    class Worker(QThread):
        def run(self) -> None:
            try:
                result["code"] = _dry_run(overlay, emit)
            except WechatSenderError as exc:
                emit(f"Dry run stopped: {exc}")
            except Exception as exc:
                emit(f"Dry run failed: {type(exc).__name__}: {exc}")

    worker = Worker()
    worker.finished.connect(lambda: QTimer.singleShot(300, app.quit))
    worker.start()
    app.exec()
    emit(f"Exit code: {result['code']}")
    return result["code"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", required=True, help="show targets without clicking")
    parser.parse_args()
    return run_dry_run()


if __name__ == "__main__":
    raise SystemExit(main())

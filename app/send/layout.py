"""Where the sender clicks inside the WeChat 4.x window.

Every target is resolved against the WeChat window client rect, never against
absolute screen pixels:

    x = left + rx * width  + dx * scale
    y = top  + ry * height + dy * scale

``rx``/``ry`` are ratios of the client rect. ``dx``/``dy`` are offsets in
logical pixels (100% DPI) for the parts of WeChat that keep a fixed size when
the window is resized: the left icon bar, the contact column, the chat header
and the bottom toolbar. ``scale`` is the backend's logical-to-native factor
(DPI / 96 on Windows, 1.0 on macOS where coordinates are already points).

This module is pure Python so the math can be tested without WeChat, Qt or a
display. Tune the numbers here; nothing else hard-codes a position.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, point: Point) -> bool:
        return self.left <= point.x < self.right and self.top <= point.y < self.bottom

    def is_close_to(self, other: "Rect", tolerance: int = 4) -> bool:
        return (
            abs(self.left - other.left) <= tolerance
            and abs(self.top - other.top) <= tolerance
            and abs(self.width - other.width) <= tolerance
            and abs(self.height - other.height) <= tolerance
        )


@dataclass(frozen=True)
class Anchor:
    rx: float
    ry: float
    dx: float = 0.0
    dy: float = 0.0

    def resolve(self, rect: Rect, scale: float = 1.0) -> Point:
        return Point(
            round(rect.left + self.rx * rect.width + self.dx * scale),
            round(rect.top + self.ry * rect.height + self.dy * scale),
        )


@dataclass(frozen=True)
class Region:
    top_left: Anchor
    bottom_right: Anchor

    def resolve(self, rect: Rect, scale: float = 1.0) -> Rect:
        a = self.top_left.resolve(rect, scale)
        b = self.bottom_right.resolve(rect, scale)
        left, right = sorted((a.x, b.x))
        top, bottom = sorted((a.y, b.y))
        return Rect(left, top, max(0, right - left), max(0, bottom - top))


@dataclass(frozen=True)
class WechatLayout:
    search_box: Anchor
    first_result: Anchor
    chat_title: Region
    message_input: Anchor
    send_button: Anchor
    min_width: int = 640
    min_height: int = 480

    def targets(self, rect: Rect, scale: float = 1.0) -> dict[str, Point]:
        return {
            "search_box": self.search_box.resolve(rect, scale),
            "first_result": self.first_result.resolve(rect, scale),
            "message_input": self.message_input.resolve(rect, scale),
            "send_button": self.send_button.resolve(rect, scale),
        }

    def check_fits(self, rect: Rect, scale: float = 1.0) -> str:
        """Return a reason the window is too small to click reliably, or ''."""
        if rect.width < self.min_width * scale or rect.height < self.min_height * scale:
            return f"微信窗口太小（{rect.width}x{rect.height}），请放大到至少 {self.min_width}x{self.min_height}"
        for name, point in self.targets(rect, scale).items():
            if not rect.contains(point):
                return f"点击位置 {name} 超出微信窗口，请检查 app/send/layout.py 中的比例"
        title = self.chat_title.resolve(rect, scale)
        if title.width <= 0 or title.height <= 0:
            return "聊天标题区域为空，请检查 app/send/layout.py 中的 chat_title"
        return ""


# WeChat 4.x on Windows at 100% DPI: ~56px icon bar, ~250px contact column,
# search box at the top of that column, 发送(S) at the bottom-right of the chat pane.
WINDOWS_LAYOUT = WechatLayout(
    # Search box at the top of the contact column.
    search_box=Anchor(0.0, 0.0, dx=160, dy=36),
    # First row under the search box once results show (below the 联系人 header).
    first_result=Anchor(0.0, 0.0, dx=165, dy=122),
    # Chat header of the right pane; starts right of the contact column and
    # stops before the header buttons so the search box text is never read.
    chat_title=Region(Anchor(0.0, 0.0, dx=322, dy=10), Anchor(1.0, 0.0, dx=-150, dy=60)),
    # Middle of the compose area, above the 发送 button row.
    message_input=Anchor(1.0, 1.0, dx=-260, dy=-95),
    send_button=Anchor(1.0, 1.0, dx=-68, dy=-30),
)

# WeChat 4.x on macOS (points). Same structure, taller top area for the
# traffic-light buttons. Not verified against a real Mac yet.
MACOS_LAYOUT = WechatLayout(
    search_box=Anchor(0.0, 0.0, dx=170, dy=40),
    first_result=Anchor(0.0, 0.0, dx=175, dy=126),
    chat_title=Region(Anchor(0.0, 0.0, dx=334, dy=12), Anchor(1.0, 0.0, dx=-150, dy=62)),
    message_input=Anchor(1.0, 1.0, dx=-260, dy=-95),
    send_button=Anchor(1.0, 1.0, dx=-68, dy=-30),
)


def native_to_logical(value: int, screen_origin: int, device_pixel_ratio: float) -> float:
    """Map one native (physical pixel) coordinate into Qt logical coordinates.

    Qt keeps each screen's top-left at its native position and scales
    distances from there by the screen's device pixel ratio.
    """
    ratio = device_pixel_ratio or 1.0
    return screen_origin + (value - screen_origin) / ratio


def normalize_title(text: str | None) -> str:
    """Keep letters and digits only.

    OCR inserts spaces between CJK characters, reads ``-`` as ``·`` or drops
    it, and picks up stray ``《`` / ``，`` at the edge of the crop.
    """
    return "".join(
        ch for ch in str(text or "") if not ch.isspace() and unicodedata.category(ch)[0] not in {"P", "S"}
    ).casefold()


def titles_match(ocr_text: str | None, remark: str) -> bool:
    expected = normalize_title(remark)
    return bool(expected) and normalize_title(ocr_text) == expected

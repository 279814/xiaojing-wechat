"""Platform-neutral click sequence that sends one reply into WeChat.

No model is involved: the sequence is fixed and every step is checked. The
backend (windows.py / macos.py) only knows how to find, focus, click, paste,
and read pixels. The overlay only draws. Any failed check aborts before the
发送 button is clicked.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, Protocol

from .layout import Point, Rect, WechatLayout, titles_match

_LOG = logging.getLogger("autosale.client.send")


class WechatSenderError(RuntimeError):
    pass


class SendBackend(Protocol):
    layout: WechatLayout

    def activate(self) -> Rect:
        """Bring WeChat to the front and return its client rect (native coordinates)."""

    def client_rect(self) -> Rect: ...

    def scale(self) -> float:
        """Logical-to-native factor for layout offsets."""

    def coords_are_physical(self) -> bool:
        """True when rects/points are physical pixels (Windows), False for points (macOS)."""

    def is_foreground(self) -> bool: ...

    def click(self, point: Point) -> None: ...

    def select_all(self) -> None: ...

    def paste(self) -> None: ...

    def read_text_candidates(self, region: Rect) -> Iterable[str]:
        """OCR readings of the region, most reliable first (lazily).

        Raise WechatSenderError when OCR is unavailable.
        """


class Clipboard(Protocol):
    def copy(self, text: str) -> None: ...

    def paste(self) -> str: ...


class Overlay(Protocol):
    def begin(self, rect: Rect, physical: bool) -> None: ...

    def move_cursor(self, point: Point, duration_ms: int) -> None: ...

    def pulse(self) -> None: ...

    def set_cursor_visible(self, visible: bool) -> None: ...

    def set_status(self, text: str) -> None: ...

    def end(self) -> None: ...


class NullOverlay:
    def begin(self, rect: Rect, physical: bool) -> None:
        pass

    def move_cursor(self, point: Point, duration_ms: int) -> None:
        pass

    def pulse(self) -> None:
        pass

    def set_cursor_visible(self, visible: bool) -> None:
        pass

    def set_status(self, text: str) -> None:
        pass

    def end(self) -> None:
        pass


class SendSequence:
    def __init__(
        self,
        backend: SendBackend,
        clipboard: Clipboard,
        overlay: Overlay | None = None,
        *,
        search_delay_ms: int = 800,
        cursor_move_ms: int = 380,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.clipboard = clipboard
        self.overlay = overlay or NullOverlay()
        self.search_delay_ms = max(200, int(search_delay_ms))
        self.cursor_move_ms = max(0, int(cursor_move_ms))
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock
        self._started = 0.0
        self._rect: Rect | None = None

    def run(self, remark: str, replies: list[str], *, auto_send: bool) -> None:
        remark = str(remark or "").strip()
        texts = [str(item or "").strip() for item in replies if str(item or "").strip()]
        if not remark:
            raise WechatSenderError("聊天对象为空")
        if not texts:
            raise WechatSenderError("回复内容为空")
        for text in texts:
            if text == remark:
                self._abort(f"回复内容与搜索词「{text}」相同，已中止发送，未操作微信")

        self._started = self._clock()
        rect = self.backend.activate()
        scale = self.backend.scale()
        problem = self.backend.layout.check_fits(rect, scale)
        if problem:
            self._abort(problem)
        self._rect = rect
        layout = self.backend.layout
        self.overlay.begin(rect, self.backend.coords_are_physical())
        try:
            self._require_foreground("激活微信后")
            self.overlay.set_status(f"正在打开「{remark}」")

            self._click("search_box", layout.search_box.resolve(rect, scale))
            self.backend.select_all()
            self._paste_text(remark, "粘贴联系人备注前")
            self._sleep(self.search_delay_ms / 1000)

            self._click("first_result", layout.first_result.resolve(rect, scale))
            self._sleep(0.7)
            self._verify_chat_title(remark, layout.chat_title.resolve(rect, scale))

            for index, text in enumerate(texts):
                self.overlay.set_status("正在粘贴回复" if len(texts) == 1 else f"正在粘贴第 {index + 1}/{len(texts)} 条回复")
                self._click("message_input", layout.message_input.resolve(rect, scale))
                self._paste_text(text, "粘贴回复前")
                self._sleep(0.25)
                if not auto_send:
                    continue
                if _normalize_newlines(self._read_clipboard()) != _normalize_newlines(text):
                    self._abort("点击发送前剪贴板内容不是待发送的回复，已中止发送")
                self.overlay.set_status("正在点击发送")
                self._click("send_button", layout.send_button.resolve(rect, scale))
                self._sleep(0.5)
        finally:
            self.overlay.end()

    def _verify_chat_title(self, remark: str, region: Rect) -> None:
        self._check_timeout()
        self._require_foreground("核对聊天标题前")
        self.overlay.set_status("正在核对聊天标题")
        self.overlay.set_cursor_visible(False)
        self._sleep(0.12)
        readings: list[str] = []
        try:
            # A reading only passes when it equals the remark exactly, so trying
            # several OCR passes raises recall without accepting a wrong chat.
            for reading in self.backend.read_text_candidates(region):
                readings.append(reading)
                if titles_match(reading, remark):
                    _LOG.info("wechat_send_title_check ok expected=%r ocr=%r", remark, reading)
                    return
        finally:
            self.overlay.set_cursor_visible(True)
        _LOG.info("wechat_send_title_check failed expected=%r ocr=%r", remark, readings)
        shown = next((text.strip() for text in readings if text.strip()), "") or "（未识别到文字）"
        self._abort(f"无法确认聊天标题是「{remark}」（识别到「{shown}」），已中止发送")

    def _click(self, name: str, point: Point) -> None:
        self._check_timeout()
        self._require_foreground(f"点击 {name} 前")
        self._require_window_unchanged(name)
        self.overlay.move_cursor(point, self.cursor_move_ms)
        self._sleep(self.cursor_move_ms / 1000)
        self._require_foreground(f"点击 {name} 前")
        self.overlay.pulse()
        self.backend.click(point)
        self._sleep(0.15)

    def _paste_text(self, text: str, step: str) -> None:
        self.clipboard.copy(text)
        if _normalize_newlines(self._read_clipboard()) != _normalize_newlines(text):
            self._abort(f"{step}剪贴板写入失败，已中止发送")
        self._require_foreground(step)
        self.backend.paste()

    def _read_clipboard(self) -> str | None:
        try:
            return self.clipboard.paste()
        except Exception:
            return None

    def _require_foreground(self, step: str) -> None:
        if not self.backend.is_foreground():
            self._abort(f"{step}微信不是前台窗口，已中止发送")

    def _require_window_unchanged(self, step: str) -> None:
        current = self.backend.client_rect()
        if self._rect is not None and not current.is_close_to(self._rect):
            self._abort(f"点击 {step} 前微信窗口被移动或缩放，已中止发送")

    def _check_timeout(self) -> None:
        if self._clock() - self._started > self.timeout_seconds:
            self._abort("微信键鼠操作超时，请确认微信窗口未被遮挡")

    @staticmethod
    def _abort(message: str) -> None:
        _LOG.warning("wechat_send_aborted reason=%s", message)
        raise WechatSenderError(message)


def _normalize_newlines(value: object) -> str | None:
    if value is None:
        return None
    return str(value).replace("\r\n", "\n")

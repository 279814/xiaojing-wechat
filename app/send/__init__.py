"""Send a ready reply into WeChat with a fixed, model-free click sequence.

Layout ratios: layout.py. Steps and safety checks: sequence.py. Platform
input: windows.py / macos.py, imported only when a send starts, so importing
the app never pulls in platform-specific modules.
"""

from __future__ import annotations

import sys

from .sequence import NullOverlay, Overlay, SendBackend, SendSequence, WechatSenderError

# Results need a moment to appear after the remark is pasted into the search box.
MIN_SEARCH_DELAY_MS = 800

__all__ = ["WechatSender", "WechatSenderError", "create_backend"]


def create_backend() -> SendBackend:
    if sys.platform == "win32":
        from .windows import WindowsBackend

        return WindowsBackend()
    if sys.platform == "darwin":
        from .macos import MacBackend

        return MacBackend()
    raise WechatSenderError("发送到微信目前只支持 Windows 和 macOS 微信客户端")


class WechatSender:
    def __init__(self, search_delay_ms: int = MIN_SEARCH_DELAY_MS, overlay: Overlay | None = None) -> None:
        self.search_delay_ms = max(MIN_SEARCH_DELAY_MS, int(search_delay_ms))
        self.overlay = overlay or NullOverlay()

    def send_reply(self, chat_query: str, reply: str, auto_send: bool = False) -> None:
        self.send_replies(chat_query, [reply], auto_send=auto_send)

    def send_replies(self, chat_query: str, replies: list[str], auto_send: bool = False) -> None:
        """Open the chat whose title equals ``chat_query``, paste, and click 发送 when ``auto_send``."""
        if not str(chat_query or "").strip():
            raise WechatSenderError("聊天对象为空")
        if not any(str(item or "").strip() for item in replies):
            raise WechatSenderError("回复内容为空")
        try:
            import pyperclip
        except Exception as exc:
            raise WechatSenderError(f"缺少剪贴板依赖 pyperclip: {exc}") from exc
        sequence = SendSequence(
            create_backend(),
            pyperclip,
            self.overlay,
            search_delay_ms=self.search_delay_ms,
        )
        sequence.run(chat_query, replies, auto_send=auto_send)

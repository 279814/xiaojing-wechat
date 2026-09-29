from __future__ import annotations

import functools
import logging
import platform
import time

# Auto-send searches this chat first so the customer search always switches
# chats; it is a search term only and must never be pasted into a chat.
DEFAULT_PRE_SEARCH_QUERY = "文件传输助手"

_LOG = logging.getLogger("autosale.client.wechat_sender")


class WechatSenderError(RuntimeError):
    pass


class WechatSender:
    """Keyboard-only WeChat send sequence of the packaged Autosale.exe, fail-closed."""

    def __init__(self, search_delay_ms: int = 450) -> None:
        self.search_delay_ms = max(100, int(search_delay_ms))
        self.operation_timeout_seconds = 24

    def send_reply(
        self,
        chat_query: str,
        reply: str,
        auto_send: bool = False,
        pre_search_query: str = "",
    ) -> None:
        self.send_replies(
            chat_query,
            [reply],
            auto_send=auto_send,
            pre_search_query=pre_search_query,
        )

    def send_replies(
        self,
        chat_query: str,
        replies: list[str],
        auto_send: bool = False,
        pre_search_query: str = "",
    ) -> None:
        if platform.system().lower() != "windows":
            raise WechatSenderError("自动发送目前只支持 Windows 微信客户端")
        if not chat_query.strip():
            raise WechatSenderError("聊天对象为空")
        texts = [str(item or "").strip() for item in replies if str(item or "").strip()]
        if not texts:
            raise WechatSenderError("回复内容为空")
        search_terms = {term.strip() for term in (chat_query, pre_search_query) if term.strip()}
        for text in texts:
            if text in search_terms:
                self._abort(f"回复内容与搜索词「{text}」相同，已中止发送，未操作微信")

        try:
            import pyperclip
            from pywinauto import Desktop
            from pywinauto.keyboard import send_keys
        except Exception as exc:
            raise WechatSenderError(f"缺少 Windows 自动化依赖: {exc}") from exc

        started = time.monotonic()
        window = self._focus_wechat_window(Desktop)
        self._check_timeout(started)
        time.sleep(0.2)
        hwnd = self._window_hwnd(window)
        if pre_search_query.strip():
            self._search_chat(send_keys, pyperclip, pre_search_query.strip(), started, hwnd)
        self._search_chat(send_keys, pyperclip, chat_query, started, hwnd)
        self._check_timeout(started)

        if not auto_send:
            self._paste_reply(
                send_keys,
                pyperclip,
                "\n\n".join(texts),
                auto_send=False,
                started=started,
                hwnd=hwnd,
            )
            return

        for index, text in enumerate(texts):
            self._paste_reply(
                send_keys,
                pyperclip,
                text,
                auto_send=True,
                started=started,
                hwnd=hwnd,
            )
            if index < len(texts) - 1:
                time.sleep(0.8)

    def _focus_wechat_window(self, desktop_factory):
        window = self._find_wechat_window(desktop_factory)
        if window is None:
            raise WechatSenderError("未找到微信窗口，请先打开并登录微信")
        try:
            window.set_focus()
        except Exception:
            try:
                window.restore()
                window.set_focus()
            except Exception as exc:
                raise WechatSenderError(f"无法激活微信窗口: {exc}") from exc
        if self._win32_is_iconic(self._window_hwnd(window)):
            # set_focus() skips restore when a minimized WeChat is already foreground.
            try:
                window.restore()
                window.set_focus()
            except Exception as exc:
                raise WechatSenderError(f"无法还原最小化的微信窗口: {exc}") from exc
        return window

    def _search_chat(self, send_keys, pyperclip, chat_query: str, started: float, hwnd: int) -> None:
        self._check_timeout(started)
        self._require_wechat_foreground(hwnd, f"搜索「{chat_query}」前")
        send_keys("^f", pause=0.02)
        time.sleep(self.search_delay_ms / 1000)
        send_keys("^a", pause=0.02)
        time.sleep(0.05)
        pyperclip.copy(chat_query)
        send_keys("^v", pause=0.02)
        time.sleep(0.2)
        self._require_wechat_foreground(hwnd, f"确认搜索「{chat_query}」前")
        send_keys("{ENTER}", pause=0.02)
        time.sleep(0.55)
        self._check_timeout(started)

    def _paste_reply(
        self,
        send_keys,
        pyperclip,
        reply: str,
        *,
        auto_send: bool,
        started: float,
        hwnd: int,
    ) -> None:
        self._check_timeout(started)
        self._require_wechat_foreground(hwnd, "粘贴回复前")
        pyperclip.copy(reply)
        self._require_clipboard_is_reply(pyperclip, reply, "粘贴回复前")
        send_keys("^v", pause=0.02)
        time.sleep(0.2)
        if auto_send:
            self._require_clipboard_is_reply(pyperclip, reply, "按 Enter 发送前")
            self._require_wechat_foreground(hwnd, "按 Enter 发送前")
            send_keys("{ENTER}", pause=0.02)
            time.sleep(0.45)
        self._check_timeout(started)

    def _require_clipboard_is_reply(self, pyperclip, reply: str, step: str) -> None:
        try:
            current = pyperclip.paste()
        except Exception:
            current = None
        if _normalize_newlines(current) != _normalize_newlines(reply):
            self._abort(f"{step}剪贴板内容不是待发送的回复，已中止发送，未按 Enter")

    def _require_wechat_foreground(self, hwnd: int, step: str) -> None:
        # WeChat 4.x draws search and compose inside one Qt HWND, so win32 can
        # only confirm that keystrokes reach a restored, foreground WeChat.
        if self._win32_is_iconic(hwnd):
            self._abort(f"{step}微信窗口仍是最小化状态，无法确认搜索框，已中止发送")
        foreground_pid = self._win32_foreground_pid()
        if not foreground_pid or foreground_pid != self._win32_window_pid(hwnd):
            self._abort(f"{step}微信不是前台窗口，无法确认搜索框/输入框焦点，已中止发送")

    @staticmethod
    def _abort(message: str) -> None:
        _LOG.warning("wechat_send_aborted reason=%s", message)
        raise WechatSenderError(message)

    def _check_timeout(self, started: float) -> None:
        if time.monotonic() - started > self.operation_timeout_seconds:
            raise WechatSenderError("微信键鼠操作超时，请确认微信窗口未被遮挡且可以搜索联系人")

    @staticmethod
    def _window_hwnd(window) -> int:
        handle = getattr(window, "handle", None)
        if not handle:
            raise WechatSenderError("无法获取微信窗口句柄")
        return int(handle)

    @staticmethod
    def _win32_is_iconic(hwnd: int) -> bool:
        return bool(_user32().IsIconic(int(hwnd)))

    @staticmethod
    def _win32_window_pid(hwnd: int) -> int:
        import ctypes
        from ctypes import wintypes

        pid = wintypes.DWORD(0)
        _user32().GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)

    @staticmethod
    def _win32_foreground_pid() -> int:
        foreground = _user32().GetForegroundWindow()
        if not foreground:
            return 0
        return WechatSender._win32_window_pid(int(foreground))

    @staticmethod
    def _find_wechat_window(desktop_factory):
        # win32 only: attaching WeChat 4.x through UIA blanks its window.
        try:
            desktop = desktop_factory(backend="win32")
        except Exception:
            return None
        patterns = [".*微信.*", ".*WeChat.*"]
        for pattern in patterns:
            try:
                wins = desktop.windows(title_re=pattern, visible_only=True)
            except Exception:
                wins = []
            for win in wins:
                try:
                    title = win.window_text()
                except Exception:
                    title = ""
                if title and ("微信" in title or "WeChat" in title):
                    return win
        return None


def _normalize_newlines(value: object) -> str | None:
    if value is None:
        return None
    return str(value).replace("\r\n", "\n")


@functools.lru_cache(maxsize=1)
def _user32():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    return user32

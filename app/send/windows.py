"""Windows backend: win32 only, through ctypes.

UI Automation is deliberately not used: attaching UIA to WeChat 4.x turns its
window white. Everything here is plain win32 (find window, focus, mouse and
keyboard input) plus GDI screen capture and the built-in Windows OCR engine
(Windows.Media.Ocr) to read the chat title.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import os
import time
from ctypes import wintypes

from .layout import WINDOWS_LAYOUT, Point, Rect, WechatLayout
from .sequence import WechatSenderError

WECHAT_TITLES = ("微信", "WeChat")
WECHAT_EXES = frozenset({"weixin.exe", "wechat.exe"})

SW_RESTORE = 9
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_A = 0x41
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SRCCOPY = 0x00CC0020
HALFTONE = 4
DIB_RGB_COLORS = 0
BI_RGB = 0
# OCR passes for the chat title: (upscale relative to 100% DPI, binarize, white
# border px). Windows OCR misses short CJK names at WeChat's ~16px title size;
# these passes, in this order, read the most synthetic WeChat titles correctly.
OCR_PASSES = (
    (2.0, True, 40),
    (3.0, False, 0),
    (2.0, True, 0),
    (4.0, True, 40),
    (3.0, True, 40),
    (4.0, False, 0),
)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


@functools.lru_cache(maxsize=1)
def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
    try:
        user32.GetDpiForWindow.restype = wintypes.UINT
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    except AttributeError:
        pass
    return user32


@functools.lru_cache(maxsize=1)
def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return kernel32


@functools.lru_cache(maxsize=1)
def _gdi32():
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.StretchBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
    ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    return gdi32


def _ensure_dpi_aware() -> None:
    # Qt already makes the process per-monitor aware; this covers CLI use so
    # win32 reports physical pixels in both cases.
    try:
        ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _window_text(hwnd: int) -> str:
    user32 = _user32()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_exe(pid: int) -> str:
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)


def _normal_area(hwnd: int) -> int:
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not _user32().GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return 0
    rc = placement.rcNormalPosition
    return max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)


def find_wechat_windows() -> list[tuple[int, str, str]]:
    """Visible top-level windows titled 微信/WeChat, best candidate first: (hwnd, title, exe)."""
    user32 = _user32()
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = _window_text(hwnd)
            if any(name in title for name in WECHAT_TITLES):
                found.append((int(hwnd), title))
        return True

    user32.EnumWindows(_collect, 0)
    ranked = []
    for hwnd, title in found:
        exe = _process_exe(_window_pid(hwnd))
        exe_match = exe in WECHAT_EXES
        exact_title = title.strip() in WECHAT_TITLES
        # A browser tab titled "微信公众平台" must not be mistaken for WeChat.
        if not exe_match and not exact_title:
            continue
        ranked.append(((exe_match, exact_title, _normal_area(hwnd)), hwnd, title, exe))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(hwnd, title, exe) for _key, hwnd, title, exe in ranked]


class WindowsBackend:
    def __init__(self, layout: WechatLayout = WINDOWS_LAYOUT) -> None:
        _ensure_dpi_aware()
        self.layout = layout
        self._hwnd = 0
        self._pid = 0

    def activate(self) -> Rect:
        windows = find_wechat_windows()
        if not windows:
            raise WechatSenderError("未找到微信窗口，请先打开并登录微信")
        self._hwnd = windows[0][0]
        self._pid = _window_pid(self._hwnd)
        user32 = _user32()
        if user32.IsIconic(self._hwnd):
            user32.ShowWindow(self._hwnd, SW_RESTORE)
        self._bring_to_front()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.is_foreground() and not user32.IsIconic(self._hwnd):
                break
            time.sleep(0.1)
        else:
            raise WechatSenderError("无法把微信切换到前台，请手动点一下微信窗口后重试")
        time.sleep(0.3)
        return self.client_rect()

    def _bring_to_front(self) -> None:
        user32 = _user32()
        kernel32 = _kernel32()
        if user32.SetForegroundWindow(self._hwnd) and self.is_foreground():
            user32.BringWindowToTop(self._hwnd)
            return
        # Windows refuses SetForegroundWindow from a background process; a
        # synthetic Alt tap plus input attach is the documented way around it.
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        own_thread = kernel32.GetCurrentThreadId()
        attached = bool(fg_thread and fg_thread != own_thread and user32.AttachThreadInput(own_thread, fg_thread, True))
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetForegroundWindow(self._hwnd)
            user32.BringWindowToTop(self._hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(own_thread, fg_thread, False)

    def client_rect(self) -> Rect:
        user32 = _user32()
        rc = wintypes.RECT()
        if not self._hwnd or not user32.GetClientRect(self._hwnd, ctypes.byref(rc)):
            raise WechatSenderError("无法读取微信窗口位置")
        origin = wintypes.POINT(0, 0)
        user32.ClientToScreen(self._hwnd, ctypes.byref(origin))
        return Rect(origin.x, origin.y, rc.right - rc.left, rc.bottom - rc.top)

    def scale(self) -> float:
        try:
            dpi = int(_user32().GetDpiForWindow(self._hwnd))
        except Exception:
            dpi = 0
        return dpi / 96.0 if dpi > 0 else 1.0

    def coords_are_physical(self) -> bool:
        return True

    def is_foreground(self) -> bool:
        foreground = _user32().GetForegroundWindow()
        return bool(foreground and self._pid and _window_pid(foreground) == self._pid)

    def click(self, point: Point) -> None:
        user32 = _user32()
        user32.SetCursorPos(point.x, point.y)
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def select_all(self) -> None:
        self._ctrl(VK_A)

    def paste(self) -> None:
        self._ctrl(VK_V)

    @staticmethod
    def _ctrl(vk: int) -> None:
        user32 = _user32()
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.02)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)

    def read_text_candidates(self, region: Rect):
        engine = _ocr_engine()
        max_dim = _ocr_max_dimension()
        scale = self.scale()
        for logical_factor, clean, border in OCR_PASSES:
            factor = min(logical_factor / scale, max_dim / max(region.width, 1), max_dim / max(region.height, 1))
            width, height, pixels = capture_bgra(region, max(factor, 0.5))
            if clean:
                pixels = _binarize(pixels)
            if border:
                width, height, pixels = _pad_white(width, height, pixels, border, max_dim)
            yield ocr_bgra(width, height, pixels, engine)


def _binarize(pixels: bytes, threshold: int = 170) -> bytes:
    """Dark text on WeChat's light header becomes pure black on white."""
    out = bytearray(len(pixels))
    for i in range(0, len(pixels), 4):
        lum = (pixels[i] * 29 + pixels[i + 1] * 150 + pixels[i + 2] * 77) >> 8
        value = 0 if lum < threshold else 255
        out[i] = out[i + 1] = out[i + 2] = value
        out[i + 3] = 255
    return bytes(out)


def _pad_white(width: int, height: int, pixels: bytes, border: int, max_dim: int) -> tuple[int, int, bytes]:
    border = max(0, min(border, (max_dim - width) // 2, (max_dim - height) // 2))
    if border == 0:
        return width, height, pixels
    new_w, new_h = width + 2 * border, height + 2 * border
    out = bytearray(b"\xff" * (new_w * new_h * 4))
    row = width * 4
    for y in range(height):
        start = ((y + border) * new_w + border) * 4
        out[start:start + row] = pixels[y * row:(y + 1) * row]
    return new_w, new_h, bytes(out)


def capture_bgra(region: Rect, upscale: float = 1.0) -> tuple[int, int, bytes]:
    """Grab a screen region (physical pixels) as top-down BGRA bytes."""
    if region.width <= 0 or region.height <= 0:
        raise WechatSenderError("截图区域为空")
    user32, gdi32 = _user32(), _gdi32()
    out_w, out_h = max(1, round(region.width * upscale)), max(1, round(region.height * upscale))
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, out_w, out_h)
    previous = gdi32.SelectObject(mem_dc, bitmap)
    try:
        gdi32.SetStretchBltMode(mem_dc, HALFTONE)
        if not gdi32.StretchBlt(
            mem_dc, 0, 0, out_w, out_h,
            screen_dc, region.left, region.top, region.width, region.height, SRCCOPY,
        ):
            raise WechatSenderError("截取微信标题区域失败")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = out_w
        header.biHeight = -out_h
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(out_w * out_h * 4)
        if not gdi32.GetDIBits(mem_dc, bitmap, 0, out_h, buf, ctypes.byref(header), DIB_RGB_COLORS):
            raise WechatSenderError("读取微信标题截图失败")
        return out_w, out_h, bytes(buf.raw)
    finally:
        gdi32.SelectObject(mem_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


def _winrt():
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter
    except Exception as exc:
        raise WechatSenderError(
            f"缺少 Windows OCR 组件，无法核对聊天标题，已中止发送: {exc}。请 pip install -r app/requirements.txt"
        ) from exc
    return Language, BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap, OcrEngine, DataWriter


def _ocr_max_dimension() -> int:
    try:
        return int(_winrt()[4].max_image_dimension) or 2600
    except WechatSenderError:
        raise
    except Exception:
        return 2600


def _ocr_engine():
    """Chinese engine first, then the user's profile languages."""
    Language, _alpha, _fmt, _bitmap, OcrEngine, _writer = _winrt()
    engine = None
    for tag in ("zh-Hans-CN", "zh-Hans", "zh-CN"):
        try:
            language = Language(tag)
            if OcrEngine.is_language_supported(language):
                engine = OcrEngine.try_create_from_language(language)
                break
        except Exception:
            continue
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise WechatSenderError("Windows 未安装中文 OCR 语言包，无法核对聊天标题，已中止发送")
    return engine


def ocr_bgra(width: int, height: int, pixels: bytes, engine=None) -> str:
    """Run Windows.Media.Ocr on top-down BGRA pixels."""
    _language, BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap, _ocr, DataWriter = _winrt()
    engine = engine or _ocr_engine()
    writer = DataWriter()
    writer.write_bytes(pixels)
    buffer = writer.detach_buffer()
    # GDI leaves the alpha byte at 0, so alpha must be ignored or the image reads as blank.
    # pywinrt 3 exposes the 5-argument overload under its own name.
    create = getattr(SoftwareBitmap, "create_copy_with_alpha_from_buffer", None) or SoftwareBitmap.create_copy_from_buffer
    bitmap = create(buffer, BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.IGNORE)

    async def _recognize():
        return await engine.recognize_async(bitmap)

    result = asyncio.run(_recognize())
    lines = [str(line.text or "") for line in (result.lines or [])]
    return "\n".join(lines) if lines else str(result.text or "")

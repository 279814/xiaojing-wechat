"""macOS backend: AppKit / AppleScript to activate WeChat, Quartz for input,
Vision for reading the chat title.

All pyobjc imports happen inside methods so importing the app never needs
them, and nothing here touches Windows-only modules.

Permissions the running Python / app must be granted in System Settings >
Privacy & Security: Accessibility (to post clicks and keystrokes) and Screen
Recording (to read the chat title).
"""

from __future__ import annotations

import subprocess
import time

from .layout import MACOS_LAYOUT, Point, Rect, WechatLayout
from .sequence import WechatSenderError

WECHAT_BUNDLE_IDS = ("com.tencent.xinWeChat",)
WECHAT_OWNER_NAMES = ("微信", "WeChat")
KEY_A = 0
KEY_V = 9


def _quartz():
    try:
        import Quartz
    except Exception as exc:
        raise WechatSenderError(
            f"缺少 pyobjc-framework-Quartz，无法在 macOS 上操作微信: {exc}。请 pip install -r app/requirements.txt"
        ) from exc
    return Quartz


class MacBackend:
    def __init__(self, layout: WechatLayout = MACOS_LAYOUT) -> None:
        self.layout = layout
        self._pid = 0

    def activate(self) -> Rect:
        if not self._activate_with_appkit():
            self._activate_with_applescript()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.is_foreground():
                break
            time.sleep(0.1)
        else:
            raise WechatSenderError("无法把微信切换到前台，请手动点一下微信窗口后重试")
        time.sleep(0.3)
        return self.client_rect()

    def _activate_with_appkit(self) -> bool:
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        except Exception:
            return False
        for bundle_id in WECHAT_BUNDLE_IDS:
            apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
            if apps and len(apps):
                app = apps[0]
                self._pid = int(app.processIdentifier())
                if app.isHidden():
                    app.unhide()
                return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        return False

    def _activate_with_applescript(self) -> None:
        for bundle_id in WECHAT_BUNDLE_IDS:
            script = f'tell application id "{bundle_id}" to activate'
            try:
                result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            except Exception as exc:
                raise WechatSenderError(f"无法激活微信: {exc}") from exc
            if result.returncode == 0:
                return
        raise WechatSenderError("未找到微信，请先打开并登录 macOS 微信")

    def client_rect(self) -> Rect:
        """Largest on-screen normal-layer WeChat window, in points (top-left origin)."""
        Quartz = _quartz()
        infos = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        best: Rect | None = None
        for info in infos:
            owner = str(info.get("kCGWindowOwnerName") or "")
            pid = int(info.get("kCGWindowOwnerPID") or 0)
            if int(info.get("kCGWindowLayer") or 0) != 0:
                continue
            if not (owner in WECHAT_OWNER_NAMES or (self._pid and pid == self._pid)):
                continue
            bounds = info.get("kCGWindowBounds") or {}
            rect = Rect(
                int(bounds.get("X", 0)),
                int(bounds.get("Y", 0)),
                int(bounds.get("Width", 0)),
                int(bounds.get("Height", 0)),
            )
            if best is None or rect.width * rect.height > best.width * best.height:
                best = rect
                self._pid = pid or self._pid
        if best is None or best.width <= 0:
            raise WechatSenderError("未找到可见的微信窗口，请先打开并登录 macOS 微信")
        return best

    def scale(self) -> float:
        return 1.0

    def coords_are_physical(self) -> bool:
        return False

    def is_foreground(self) -> bool:
        try:
            from AppKit import NSWorkspace
        except Exception:
            return self._frontmost_via_applescript()
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return False
        if str(app.bundleIdentifier() or "") in WECHAT_BUNDLE_IDS:
            self._pid = int(app.processIdentifier())
            return True
        return False

    @staticmethod
    def _frontmost_via_applescript() -> bool:
        script = 'tell application "System Events" to get bundle identifier of first process whose frontmost is true'
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        return result.stdout.strip() in WECHAT_BUNDLE_IDS

    def click(self, point: Point) -> None:
        Quartz = _quartz()
        position = (float(point.x), float(point.y))
        for event_type in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            event = Quartz.CGEventCreateMouseEvent(None, event_type, position, Quartz.kCGMouseButtonLeft)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.04)

    def select_all(self) -> None:
        self._command(KEY_A)

    def paste(self) -> None:
        self._command(KEY_V)

    @staticmethod
    def _command(keycode: int) -> None:
        Quartz = _quartz()
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
            Quartz.CGEventSetFlags(event, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            time.sleep(0.03)
        time.sleep(0.05)

    def read_text_candidates(self, region: Rect):
        yield self._read_text(region)

    def _read_text(self, region: Rect) -> str:
        Quartz = _quartz()
        try:
            import Vision
        except Exception as exc:
            raise WechatSenderError(
                f"缺少 pyobjc-framework-Vision，无法核对聊天标题，已中止发送: {exc}"
            ) from exc
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(region.left, region.top, region.width, region.height),
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image is None:
            raise WechatSenderError("无法截取微信标题区域，请在系统设置中授予屏幕录制权限")
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["zh-Hans", "en-US"])
        request.setUsesLanguageCorrection_(False)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, {})
        ok, error = handler.performRequests_error_([request], None)
        if not ok:
            raise WechatSenderError(f"macOS 文字识别失败: {error}")
        lines = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if candidates and len(candidates):
                lines.append(str(candidates[0].string()))
        return "\n".join(lines)

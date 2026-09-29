import unittest

from app.send.layout import WINDOWS_LAYOUT, Point, Rect
from app.send.sequence import SendSequence, WechatSenderError


class FakeBackend:
    layout = WINDOWS_LAYOUT

    def __init__(self, title="张三顾客", foreground=True):
        self.titles = [title] if isinstance(title, str) else list(title)
        self.foreground = foreground
        self.rect = Rect(0, 0, 1000, 720)
        self.events = []

    def activate(self):
        self.events.append("activate")
        return self.rect

    def client_rect(self):
        return self.rect

    def scale(self):
        return 1.0

    def coords_are_physical(self):
        return True

    def is_foreground(self):
        return self.foreground

    def click(self, point):
        self.events.append(("click", self._name(point)))

    def select_all(self):
        self.events.append("select_all")

    def paste(self):
        self.events.append("paste")

    def read_text_candidates(self, region):
        for title in self.titles:
            self.events.append("read_title")
            yield title

    def _name(self, point: Point) -> str:
        for name, target in self.layout.targets(self.rect).items():
            if target == point:
                return name
        return f"{point.x},{point.y}"


class FakeClipboard:
    def __init__(self):
        self.value = ""
        self.history = []

    def copy(self, text):
        self.value = text
        self.history.append(text)

    def paste(self):
        return self.value


def _sequence(backend, clipboard=None):
    return SendSequence(backend, clipboard or FakeClipboard(), sleep=lambda _s: None)


def _clicks(backend):
    return [event[1] for event in backend.events if isinstance(event, tuple)]


class SendSequenceTest(unittest.TestCase):
    def test_auto_send_click_order(self):
        backend, clipboard = FakeBackend(), FakeClipboard()
        _sequence(backend, clipboard).run("张三顾客", ["您好"], auto_send=True)
        self.assertEqual(_clicks(backend), ["search_box", "first_result", "message_input", "send_button"])
        self.assertEqual(clipboard.history, ["张三顾客", "您好"])
        self.assertLess(backend.events.index("read_title"), backend.events.index(("click", "message_input")))

    def test_manual_mode_pastes_without_clicking_send(self):
        backend = FakeBackend()
        _sequence(backend).run("张三顾客", ["您好"], auto_send=False)
        self.assertNotIn("send_button", _clicks(backend))
        self.assertEqual(backend.events[-1], "paste")

    def test_title_mismatch_aborts_before_input(self):
        backend, clipboard = FakeBackend(title="张三丰顾客"), FakeClipboard()
        with self.assertRaises(WechatSenderError):
            _sequence(backend, clipboard).run("张三顾客", ["您好"], auto_send=True)
        self.assertEqual(_clicks(backend), ["search_box", "first_result"])
        self.assertNotIn("您好", clipboard.history)

    def test_later_ocr_pass_can_confirm_title(self):
        backend = FakeBackend(title=["", "张 客", "张 三 顾 客", "never read"])
        _sequence(backend).run("张三顾客", ["您好"], auto_send=True)
        self.assertEqual(backend.events.count("read_title"), 3)
        self.assertIn("send_button", _clicks(backend))

    def test_all_ocr_passes_partial_aborts(self):
        backend = FakeBackend(title=["", "张 客", "张三"])
        with self.assertRaises(WechatSenderError):
            _sequence(backend).run("张三顾客", ["您好"], auto_send=True)
        self.assertNotIn("message_input", _clicks(backend))

    def test_empty_reply_never_touches_wechat(self):
        backend = FakeBackend()
        with self.assertRaises(WechatSenderError):
            _sequence(backend).run("张三顾客", ["", "   "], auto_send=True)
        self.assertEqual(backend.events, [])

    def test_background_wechat_aborts(self):
        backend = FakeBackend(foreground=False)
        with self.assertRaises(WechatSenderError):
            _sequence(backend).run("张三顾客", ["您好"], auto_send=True)
        self.assertEqual(_clicks(backend), [])

    def test_multiple_replies_each_sent(self):
        backend = FakeBackend()
        _sequence(backend).run("张三顾客", ["第一条", "第二条"], auto_send=True)
        self.assertEqual(_clicks(backend).count("send_button"), 2)


if __name__ == "__main__":
    unittest.main()

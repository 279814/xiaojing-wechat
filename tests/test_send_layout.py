import unittest

from app.send.layout import (
    MACOS_LAYOUT,
    WINDOWS_LAYOUT,
    Anchor,
    Point,
    Rect,
    Region,
    native_to_logical,
    titles_match,
)


class AnchorMathTest(unittest.TestCase):
    def test_pure_ratio_is_relative_to_client_rect(self):
        rect = Rect(100, 50, 1000, 800)
        self.assertEqual(Anchor(0.5, 0.25).resolve(rect), Point(600, 250))
        self.assertEqual(Anchor(0.0, 0.0).resolve(rect), Point(100, 50))
        self.assertEqual(Anchor(1.0, 1.0).resolve(rect), Point(1100, 850))

    def test_same_ratio_follows_the_window_when_it_moves(self):
        anchor = Anchor(0.3, 0.6)
        a = anchor.resolve(Rect(0, 0, 1000, 700))
        b = anchor.resolve(Rect(-1920, 200, 1000, 700))
        self.assertEqual((b.x - a.x, b.y - a.y), (-1920, 200))

    def test_offsets_scale_with_dpi(self):
        rect = Rect(0, 0, 2000, 1400)
        anchor = Anchor(1.0, 1.0, dx=-68, dy=-30)
        self.assertEqual(anchor.resolve(rect, 1.0), Point(1932, 1370))
        self.assertEqual(anchor.resolve(rect, 1.5), Point(1898, 1355))
        self.assertEqual(anchor.resolve(rect, 2.0), Point(1864, 1340))

    def test_rounding_to_nearest_pixel(self):
        self.assertEqual(Anchor(1 / 3, 2 / 3).resolve(Rect(0, 0, 100, 100)), Point(33, 67))

    def test_region_normalizes_corners(self):
        region = Region(Anchor(1.0, 0.0, dx=-150, dy=60), Anchor(0.0, 0.0, dx=322, dy=10))
        self.assertEqual(region.resolve(Rect(10, 20, 1000, 700)), Rect(332, 30, 528, 50))


class DefaultLayoutTest(unittest.TestCase):
    def test_all_targets_inside_typical_windows(self):
        for layout in (WINDOWS_LAYOUT, MACOS_LAYOUT):
            for rect, scale in (
                (Rect(0, 0, 1000, 720), 1.0),
                (Rect(-1600, 100, 1500, 1000), 1.25),
                (Rect(0, 0, 2880, 1700), 2.0),
            ):
                with self.subTest(rect=rect, scale=scale):
                    self.assertEqual(layout.check_fits(rect, scale), "")

    def test_search_above_first_result_and_input_above_send_row(self):
        rect, scale = Rect(0, 0, 1000, 720), 1.0
        t = WINDOWS_LAYOUT.targets(rect, scale)
        self.assertLess(t["search_box"].y, t["first_result"].y)
        self.assertLess(t["message_input"].y, t["send_button"].y)
        self.assertLess(t["message_input"].x, t["send_button"].x)
        self.assertLess(t["first_result"].x, WINDOWS_LAYOUT.chat_title.resolve(rect, scale).left)

    def test_title_region_does_not_overlap_search_box(self):
        rect = Rect(0, 0, 1000, 720)
        title = WINDOWS_LAYOUT.chat_title.resolve(rect)
        self.assertFalse(title.contains(WINDOWS_LAYOUT.search_box.resolve(rect)))

    def test_tiny_window_is_rejected(self):
        self.assertNotEqual(WINDOWS_LAYOUT.check_fits(Rect(0, 0, 400, 300), 1.0), "")


class DpiAndTitleTest(unittest.TestCase):
    def test_native_to_logical(self):
        self.assertEqual(native_to_logical(300, 0, 1.5), 200)
        self.assertEqual(native_to_logical(2220, 1920, 1.5), 2120)
        self.assertEqual(native_to_logical(500, 0, 1.0), 500)

    def test_titles_match_ignores_ocr_spacing_only(self):
        self.assertTrue(titles_match("张 三 顾客", "张三顾客"))
        self.assertTrue(titles_match("张三顾客\n", " 张三顾客 "))
        self.assertFalse(titles_match("张三丰顾客", "张三顾客"))
        self.assertFalse(titles_match("张三顾客(2)", "张三顾客"))
        self.assertFalse(titles_match("", "张三顾客"))
        self.assertFalse(titles_match("张三", ""))

    def test_titles_match_ignores_ocr_punctuation_noise(self):
        self.assertTrue(titles_match("李 女 士 · 顾 客", "李女士-顾客"))
        self.assertTrue(titles_match("《 孙 尚 香", "孙尚香"))
        self.assertTrue(titles_match("王小明 顾客a", "王小明顾客A"))
        self.assertFalse(titles_match("孙 尚 杳", "孙尚香"))
        self.assertFalse(titles_match("---", "---"))

    # Windows OCR passes of the real WeChat title 顾客-黑大帅 at 150% DPI.
    REAL_OCR_PASSES = (
        "顶 客 · 黑 大 帅",
        "顶 客 一 黑 大 帅",
        "客 · 黑 大 帅",
        "顶 客 · 黑 大 帅",
        "顶 客 一 黑 大 帅",
        "人 客 · 黑 大 帅",
    )

    def test_real_ocr_passes_match_remark(self):
        for reading in self.REAL_OCR_PASSES:
            with self.subTest(reading=reading):
                self.assertTrue(titles_match(reading, "顾客-黑大帅"))

    def test_separator_variants_match(self):
        for reading in ("顾客-黑大帅", "顾客黑大帅", "顾客—黑大帅", "顾客–黑大帅", "顾 客 一 黑 大 帅", "顾客·黑大帅"):
            with self.subTest(reading=reading):
                self.assertTrue(titles_match(reading, "顾客-黑大帅"))

    def test_other_contacts_are_rejected(self):
        for reading in (
            "顾客-白大帅",
            "文件传输助手",
            "搜索网络结果",
            "",
            "   ",
            "黑大帅",
            "顾人-黑大帅",
            "张三-黑大帅",
            "顶客一黑大帅一",
            "顾客-黑大帅的朋友圈",
            "顾客-黑大帅 和 其他 群 成 员 的 群 聊 名 称",
        ):
            with self.subTest(reading=reading):
                self.assertFalse(titles_match(reading, "顾客-黑大帅"))

    def test_dash_letter_only_accepted_at_separator(self):
        self.assertFalse(titles_match("王博", "王一博"))
        self.assertTrue(titles_match("王一博", "王一博"))
        self.assertFalse(titles_match("顾一客-黑大帅", "顾客-黑大帅"))

    def test_first_glyph_slack_needs_three_matching_characters(self):
        self.assertFalse(titles_match("三", "张三"))
        self.assertFalse(titles_match("李三", "张三"))
        self.assertFalse(titles_match("客a", "顾客a"))
        self.assertTrue(titles_match("客ab", "顾客ab"))


class RealWindowTitleCropTest(unittest.TestCase):
    # Dry-run rect of the user's WeChat window at 150% DPI.
    RECT, SCALE = Rect(518, 263, 1331, 1058), 1.5

    def test_crop_starts_right_of_contact_column_and_before_title_glyph(self):
        title = WINDOWS_LAYOUT.chat_title.resolve(self.RECT, self.SCALE)
        contact_column_right = self.RECT.left + round(301 * self.SCALE)
        first_glyph_left = self.RECT.left + round(318 * self.SCALE)
        self.assertGreater(title.left, contact_column_right)
        self.assertLess(title.left, first_glyph_left)

    def test_crop_avoids_window_buttons(self):
        title = WINDOWS_LAYOUT.chat_title.resolve(self.RECT, self.SCALE)
        pin_left = self.RECT.right - round(160 * self.SCALE)
        pin_row_bottom = self.RECT.top + round(23 * self.SCALE)
        self.assertLessEqual(title.right, pin_left)
        self.assertGreater(title.top, pin_row_bottom)


if __name__ == "__main__":
    unittest.main()

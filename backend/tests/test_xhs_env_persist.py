import time
import unittest


from app.services.xhs_crawler import (
    _cookies_to_header,
    _dedupe_content_text,
    _prefer_split_title_content,
    _strip_leading_title_from_content,
    _upsert_env_line,
)


class TestEnvUpsert(unittest.TestCase):
    def test_upsert_adds_when_missing(self):
        lines = ["A=1", "B=2"]
        out = _upsert_env_line(lines, "XHS_COOKIE", "a=1; b=2")
        self.assertEqual(out[-1], "XHS_COOKIE=a=1; b=2")
        self.assertIn("A=1", out)
        self.assertIn("B=2", out)

    def test_upsert_replaces_existing(self):
        lines = ["XHS_COOKIE=old", "A=1"]
        out = _upsert_env_line(lines, "XHS_COOKIE", "newcookie")
        self.assertEqual(out[0], "XHS_COOKIE=newcookie")
        self.assertEqual(out[1], "A=1")

    def test_upsert_dedupes_duplicates(self):
        lines = ["XHS_COOKIE=old", "XHS_COOKIE=older", "A=1"]
        out = _upsert_env_line(lines, "XHS_COOKIE", "newcookie")
        self.assertEqual(out.count("XHS_COOKIE=newcookie"), 1)
        self.assertNotIn("XHS_COOKIE=old", out)
        self.assertNotIn("XHS_COOKIE=older", out)


class TestCookieHeader(unittest.TestCase):
    def test_cookie_header_sorted_and_filters_expired(self):
        now = time.time()
        cookies = [
            {"name": "b", "value": "2", "expires": -1},
            {"name": "a", "value": "1", "expires": now + 3600},
            {"name": "expired", "value": "x", "expires": now - 10},
        ]
        header = _cookies_to_header(cookies)
        self.assertEqual(header, "a=1; b=2")

    def test_cookie_header_skips_empty(self):
        cookies = [
            {"name": "", "value": "1", "expires": -1},
            {"name": "a", "value": "", "expires": -1},
            {"name": "b", "value": "2", "expires": -1},
        ]
        header = _cookies_to_header(cookies)
        self.assertEqual(header, "b=2")


class TestTitleDedup(unittest.TestCase):
    def test_strip_leading_title_from_content(self):
        title = "小酌一瓶 ｜劲酒125ml - 小红书"
        content = "小酌一瓶 ｜劲酒125ml\n今天尝了一下，口感还行。"
        out = _strip_leading_title_from_content(title, content)
        self.assertEqual(out, "今天尝了一下，口感还行。")

    def test_dedupe_content_and_strip_date(self):
        title = "品中国劲酒，打亲朋好友。"
        content = (
            "品中国劲酒，打亲朋好友。 难喝哦🙄#年轻人喝劲酒 #养生酒的天花板\n"
            "品中国劲酒，打亲朋好友。\n"
            "难喝哦🙄#年轻人喝劲酒 #养生酒的天花板\n"
            "2025-09-09\n"
        )
        out = _dedupe_content_text(title, content)
        # Content should not repeat the title (title is shown separately via reference_text).
        self.assertEqual(out, "难喝哦🙄#年轻人喝劲酒 #养生酒的天花板")

    def test_prefer_split_title_over_merged(self):
        title = "品中国劲酒，打亲朋好友。 难喝哦🙄#年轻人喝劲酒 #养生酒的天花板"
        content = "品中国劲酒，打亲朋好友。\n难喝哦🙄#年轻人喝劲酒 #养生酒的天花板"
        t2, c2 = _prefer_split_title_content(title, content)
        self.assertEqual(t2, "品中国劲酒，打亲朋好友。")
        self.assertEqual(c2, "难喝哦🙄#年轻人喝劲酒 #养生酒的天花板")


if __name__ == "__main__":
    unittest.main()

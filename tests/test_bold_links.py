"""
Bold Link toggle ke test.

    python -m unittest tests/test_bold_links.py -v
"""
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import caption   # noqa: E402
import engine    # noqa: E402
import task_ui   # noqa: E402

TEXT = "Deal https://www.amazon.in/dp/B0AAAAAAA1 now"


def ent(t, off, ln, url=None):
    return types.SimpleNamespace(type=t, offset=off, length=ln, url=url)


URL_ENT = [ent("url", 5, 35)]


class BoldLinkTest(unittest.TestCase):
    def test_url_bold_on_by_default(self):
        self.assertIn("<b>https://www.amazon.in/dp/B0AAAAAAA1</b>", engine.entities_to_html(TEXT, URL_ENT))

    def test_url_not_bold_when_off(self):
        out = engine.entities_to_html(TEXT, URL_ENT, bold_links=False)
        self.assertNotIn("<b>", out)
        self.assertIn("https://www.amazon.in/dp/B0AAAAAAA1", out)

    def test_hidden_link_keeps_href_without_bold(self):
        t = "Buy here now"
        out = engine.entities_to_html(t, [ent("text_link", 4, 4, "https://x.y/z")], bold_links=False)
        self.assertIn('<a href="https://x.y/z">here</a>', out)

    def test_other_bold_text_untouched(self):
        out = engine.entities_to_html("Hot deal", [ent("bold", 0, 3)], bold_links=False)
        self.assertIn("<b>Hot</b>", out)                # normal bold text pe asar nahi

    def test_amazon_caption_link(self):
        prod = {"asin": "B0AAAAAAA1", "title": "Item", "deal_price": "₹199", "actual_price": "₹850",
                "discount_pct": 77}
        link = "https://www.amazon.in/dp/B0AAAAAAA1?tag=dk-21"
        cfg_on = dict(engine.DEFAULT_TASK) if hasattr(engine, "DEFAULT_TASK") else {}
        on, _ = caption.build_amazon_caption(prod, link, dict(cfg_on, bold_links=True), has_image=True)
        off, _ = caption.build_amazon_caption(prod, link, dict(cfg_on, bold_links=False), has_image=True)
        self.assertIn("<b>https://www.amazon.in/dp/B0AAAAAAA1?tag=dk-21</b>", on)
        self.assertIn("🛒 https://www.amazon.in/dp/B0AAAAAAA1?tag=dk-21", off)

    def test_toggle_screen_and_button(self):
        task = {"id": 3, "paused": False, "cfg": {"name": "T", "bold_links": False}}
        self.assertIn("❌ OFF", task_ui.bold_text(task, "hi"))


if __name__ == "__main__":
    unittest.main()

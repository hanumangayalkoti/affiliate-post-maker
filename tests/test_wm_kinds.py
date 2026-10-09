"""
Watermark: Amazon / Non-Amazon posts ka alag ON/OFF.

    python -m unittest tests/test_wm_kinds.py -v
"""
import asyncio
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import engine   # noqa: E402
import task_ui  # noqa: E402

WM = {"enabled": True, "text": "@MyDeals", "position": "bottom_right", "size": "s", "color": "white"}


class WmKindTest(unittest.TestCase):
    def test_defaults_both_on(self):
        cfg = {"watermark": WM}
        self.assertTrue(engine._wm_on(cfg, "amazon"))
        self.assertTrue(engine._wm_on(cfg, "other"))

    def test_each_toggle(self):
        cfg = {"watermark": WM, "wm_amazon": False, "wm_other": True}
        self.assertFalse(engine._wm_on(cfg, "amazon"))
        self.assertTrue(engine._wm_on(cfg, "other"))

    def test_master_off_wins(self):
        cfg = {"watermark": dict(WM, enabled=False), "wm_amazon": True}
        self.assertFalse(engine._wm_on(cfg, "amazon"))

    def test_amazon_card_gets_no_watermark_when_off(self):
        seen = {}

        def fake_card(raw, product, card_cfg, wm, badge):
            seen["wm"] = wm
            return b"card"
        orig = engine.render_card
        engine.render_card = fake_card
        try:
            cfg = {"watermark": WM, "wm_amazon": False, "card": {"enabled": True}}
            out, used = asyncio.run(engine.make_post_image(b"raw", {}, cfg, True))
        finally:
            engine.render_card = orig
        self.assertEqual((out, used), (b"card", True))
        self.assertEqual(seen["wm"], {})

    def test_amazon_card_keeps_watermark_when_on(self):
        seen = {}

        def fake_card(raw, product, card_cfg, wm, badge):
            seen["wm"] = wm
            return b"card"
        orig = engine.render_card
        engine.render_card = fake_card
        try:
            asyncio.run(engine.make_post_image(b"raw", {}, {"watermark": WM, "card": {"enabled": True}}, True))
        finally:
            engine.render_card = orig
        self.assertEqual(seen["wm"]["text"], "@MyDeals")

    def test_screen_and_buttons(self):
        task = {"id": 5, "cfg": {"name": "T", "watermark": WM, "wm_amazon": True, "wm_other": False}}
        text = task_ui.wm_text(task, "hi")
        self.assertIn("Amazon ✅", text)
        self.assertIn("Non-Amazon ❌", text)
        rows = [[b.callback_data for b in r] for r in task_ui.wm_kb(task, "hi").inline_keyboard]
        self.assertIn(["t:5:wmk:amazon", "t:5:wmk:other"], rows)


if __name__ == "__main__":
    unittest.main()

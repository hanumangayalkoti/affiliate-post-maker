"""
Discount Filter (sirf Amazon) ke test — asli Telegram / Amazon API ke bina.

    python -m unittest tests/test_discount_filter.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import engine  # noqa: E402

A1 = "https://www.amazon.in/dp/B0AAAAAAA1"
A2 = "https://www.amazon.in/dp/B0AAAAAAA2"


def make_task(**cfg):
    base = {"name": "T", "tag": "dk-21", "channel": "-1005", "channel_title": "Ch",
            "allow_amazon": True, "allow_other": True, "amz_detailed": True, "min_discount": 50}
    base.update(cfg)
    return {"id": 1, "user_id": 7, "cfg": base}


class DiscountFilterTest(unittest.TestCase):
    def run_case(self, text, task, data):
        """data: {asin: discount%}  — jo ASIN isme nahi, uska Amazon data nahi mila."""
        self.posted, self.original, self.other, replies = [], [], [], []

        async def notify(t, **kw):
            replies.append(t)
            return None

        async def amazon_urls(urls):
            return [u for u in urls if "amazon" in u]

        async def products_by_asins(asins):
            return {a: {"asin": a, "title": f"Item {a[-1]}", "discount_pct": data[a]}
                    for a in asins if a in data}

        async def post_product(context, uid, task_, prod, lang):
            self.posted.append(prod["asin"])
            return "posted", prod["title"], ""

        async def post_original(context, uid, task_, msg, *a, **k):
            self.original.append(True)
            return "posted", "orig", ""

        async def post_other(*a, **k):
            self.other.append(True)
            return "posted", "ok"

        async def fallback_send(*a, **k):
            self.posted.append("fallback")

        engine.get_amazon_urls_deep = amazon_urls
        engine.get_products_by_asins = products_by_asins
        engine.post_amazon_product = post_product
        engine.post_amazon_original = post_original
        engine.post_other = post_other
        engine.setup_problems = lambda cfg, lang: []
        engine.posts_left_today = lambda uid, tid=None: 100
        self.claims = []
        engine.claim_posted = lambda *a: self.claims.append(a) or (True, None)
        engine.log_post = lambda *a, **k: None
        engine.deliver = fallback_send
        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(send_message=None))
        msg = types.SimpleNamespace(caption=None, text=text, entities=[], caption_entities=[],
                                    photo=None, document=None, video=None, animation=None,
                                    video_note=None)
        asyncio.run(engine.process_and_post(ctx, 7, msg, notify, make_task_ref[0], "hi"))
        return replies

    def go(self, text, data, **cfg):
        make_task_ref[0] = make_task(**cfg)
        return self.run_case(text, make_task_ref[0], data)

    def test_low_discount_skipped(self):
        replies = self.go(f"Deal {A1}", {"B0AAAAAAA1": 30})
        self.assertEqual(self.posted, [])
        self.assertIn("discount kam", replies[-1])
        self.assertIn("30%", replies[-1])

    def test_high_discount_posted(self):
        self.go(f"Deal {A1}", {"B0AAAAAAA1": 65})
        self.assertEqual(self.posted, ["B0AAAAAAA1"])

    def test_exactly_at_filter_is_posted(self):
        self.go(f"Deal {A1}", {"B0AAAAAAA1": 50})
        self.assertEqual(self.posted, ["B0AAAAAAA1"])

    def test_detailed_each_product_checked(self):
        replies = self.go(f"{A1}\n{A2}", {"B0AAAAAAA1": 70, "B0AAAAAAA2": 20})
        self.assertEqual(self.posted, ["B0AAAAAAA1"])
        self.assertIn("1 skip", replies[-1])

    def test_no_data_filter_on_skipped_not_claimed(self):
        replies = self.go(f"Deal {A1}", {})            # filter 50% ON, API ne data nahi diya
        self.assertEqual(self.posted, [])              # post NAHI hua
        self.assertEqual(self.claims, [])              # duplicate mein nahi gina
        self.assertIn("details nahi mili", replies[-1])
        self.assertIn("50%+ ON", replies[-1])
        self.assertIn("B0AAAAAAA1", replies[-1])

    def test_no_data_filter_off_still_posted(self):
        self.go(f"Deal {A1}", {}, min_discount=0)
        self.assertEqual(self.posted, ["fallback"])    # pehle jaisa original text + tag

    def test_minimal_no_data_filter_on_skipped(self):
        replies = self.go(f"Deal {A1}", {}, amz_detailed=False)
        self.assertEqual(self.original, [])
        self.assertIn("details nahi mili", replies[-1])

    def test_minimal_no_data_filter_off_posted(self):
        self.go(f"Deal {A1}", {}, amz_detailed=False, min_discount=0)
        self.assertEqual(self.original, [True])

    def test_non_amazon_never_filtered(self):
        self.go("Flipkart deal https://fkrt.it/x", {})
        self.assertEqual(self.other, [True])

    def test_minimal_any_one_passes(self):
        self.go(f"{A1}\n{A2}", {"B0AAAAAAA1": 70, "B0AAAAAAA2": 20}, amz_detailed=False)
        self.assertEqual(self.original, [True])

    def test_minimal_all_low_skipped(self):
        replies = self.go(f"{A1}\n{A2}", {"B0AAAAAAA1": 10, "B0AAAAAAA2": 20}, amz_detailed=False)
        self.assertEqual(self.original, [])
        self.assertIn("20%", replies[-1])              # sabse zyada discount dikhaya

    def test_filter_off_posts_everything(self):
        self.go(f"Deal {A1}", {"B0AAAAAAA1": 5}, min_discount=0)
        self.assertEqual(self.posted, ["B0AAAAAAA1"])


make_task_ref = [None]

if __name__ == "__main__":
    unittest.main()

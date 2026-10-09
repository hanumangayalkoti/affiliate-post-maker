"""
Post queue mein thi aur beech mein task delete ho gaya → post NAHI jaani chahiye.

    python -m unittest tests/test_deleted_task.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import engine  # noqa: E402

PATCH = ("get_amazon_urls_deep", "get_products_by_asins", "post_amazon_product", "post_other",
         "setup_problems", "posts_left_today", "get_task")


class DeletedTaskTest(unittest.TestCase):
    def setUp(self):
        self._o = {n: getattr(engine, n) for n in PATCH}

    def tearDown(self):
        for n, f in self._o.items():
            setattr(engine, n, f)

    def run_case(self, text, exists=True):
        posted, replies = [], []

        async def notify(t, **kw):
            replies.append(t)

        async def amazon_urls(urls):
            return [u for u in urls if "amazon" in u]

        async def products(asins):
            return {a: {"asin": a, "title": "X", "discount_pct": 60} for a in asins}

        async def post_amz(context, uid, task, prod, lang):
            posted.append("amazon")
            return "posted", "X", ""

        async def post_oth(context, uid, task, payload, lang, **kw):
            posted.append("other")
            return "posted", "ok"

        cfg = {"name": "T", "tag": "dk-21", "channel": "-1005", "allow_amazon": True, "allow_other": True,
               "amz_detailed": True}
        task = {"id": 1, "user_id": 7, "cfg": cfg}
        engine.get_amazon_urls_deep = amazon_urls
        engine.get_products_by_asins = products
        engine.post_amazon_product = post_amz
        engine.post_other = post_oth
        engine.setup_problems = lambda c, lang: []
        engine.posts_left_today = lambda uid, tid=None: 100
        engine.get_task = (lambda tid, uid: task) if exists else (lambda tid, uid: None)
        msg = types.SimpleNamespace(caption=None, text=text, entities=[], caption_entities=[], photo=None,
                                    document=None, video=None, animation=None, video_note=None)
        asyncio.run(engine.process_and_post(types.SimpleNamespace(bot=None), 7, msg, notify, task, "hi"))
        return posted, replies

    def test_amazon_deleted_task_not_posted(self):
        posted, replies = self.run_case("Deal https://www.amazon.in/dp/B0AAAAAAA1", exists=False)
        self.assertEqual(posted, [])
        self.assertIn("delete ho chuka", replies[-1])

    def test_non_amazon_deleted_task_not_posted(self):
        posted, replies = self.run_case("Myntra deal https://myntr.it/abc", exists=False)
        self.assertEqual(posted, [])
        self.assertIn("delete ho chuka", replies[-1])

    def test_existing_task_posts(self):
        self.assertEqual(self.run_case("Deal https://www.amazon.in/dp/B0AAAAAAA1")[0], ["amazon"])
        self.assertEqual(self.run_case("Myntra deal https://myntr.it/abc")[0], ["other"])


if __name__ == "__main__":
    unittest.main()

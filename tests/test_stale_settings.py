"""
Purani copy se settings mit jaane wala bug + taaza settings + Draft reply info.

    python -m unittest tests/test_stale_settings.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import alerts  # noqa: E402
import engine  # noqa: E402

A1 = "https://www.amazon.in/dp/B0AAAAAAA1"


class InviteLinkPatchTest(unittest.TestCase):
    def setUp(self):
        self._orig = alerts.patch_task_cfg

    def tearDown(self):
        alerts.patch_task_cfg = self._orig

    def test_invite_link_saves_only_its_keys(self):
        calls = []
        alerts.patch_task_cfg = lambda uid, tid, patch: calls.append((uid, tid, patch)) or True
        self.assertFalse(hasattr(alerts, "save_task"))     # poora cfg kabhi save nahi

        async def invite(chat_id, name):
            return types.SimpleNamespace(invite_link="https://t.me/+abc")
        bot = types.SimpleNamespace(create_chat_invite_link=invite)
        # Purani copy: isme min_discount 0 hai — wo DB mein nahi jaana chahiye
        task = {"id": 3, "cfg": {"channel": "-1009", "channel_title": "Pvt", "min_discount": 0}}
        out = asyncio.run(alerts._channel_link(bot, 7, task, "dest"))
        self.assertIn("t.me/+abc", out)
        self.assertEqual(calls, [(7, 3, {"dest_invite": "https://t.me/+abc", "dest_invite_for": "-1009"})])


_PATCHED = ("get_amazon_urls_deep", "get_products_by_asins", "post_amazon_product",
            "setup_problems", "posts_left_today", "get_task")


class FreshSettingsTest(unittest.TestCase):
    def setUp(self):
        self._orig = {n: getattr(engine, n) for n in _PATCHED}

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(engine, n, f)

    def run_case(self, stale_cfg, fresh_cfg, disc, source_tag=""):
        posted, replies = [], []

        async def notify(t, **kw):
            replies.append(t)
            return None

        async def amazon_urls(urls):
            return [u for u in urls if "amazon" in u]

        async def products_by_asins(asins):
            return {a: {"asin": a, "title": "Vaseline Lotion", "discount_pct": disc,
                        "deal_price": "₹199", "actual_price": "₹850"} for a in asins}

        async def post_product(context, uid, task_, prod, lang):
            posted.append(task_["cfg"].get("min_discount"))
            return "posted", prod["title"], "Image Card"

        engine.get_amazon_urls_deep = amazon_urls
        engine.get_products_by_asins = products_by_asins
        engine.post_amazon_product = post_product
        engine.setup_problems = lambda cfg, lang: []
        engine.posts_left_today = lambda uid, tid=None: 100

        base = {"name": "T", "tag": "dk-21", "channel": "-1005", "channel_title": "Ch",
                "allow_amazon": True, "allow_other": True, "amz_detailed": True}
        task = {"id": 1, "user_id": 7, "cfg": dict(base, **stale_cfg)}
        engine.get_task = lambda tid, uid: {"id": tid, "user_id": uid, "cfg": dict(base, **fresh_cfg)}
        msg = types.SimpleNamespace(caption=None, text="Deal " + A1, entities=[], caption_entities=[],
                                    photo=None, document=None, video=None, animation=None, video_note=None)
        ctx = types.SimpleNamespace(bot=None)
        asyncio.run(engine.process_and_post(ctx, 7, msg, notify, task, "hi", source_tag=source_tag))
        return posted, replies

    def test_filter_set_while_post_waiting_is_applied(self):
        # Post aate waqt filter OFF tha, beech mein user ne 80% kiya → 77% skip
        posted, replies = self.run_case({"min_discount": 0}, {"min_discount": 80}, 77)
        self.assertEqual(posted, [])
        self.assertIn("77%", replies[-1])
        self.assertIn("80%", replies[-1])

    def test_reply_is_informative(self):
        posted, replies = self.run_case({}, {"min_discount": 50}, 77, source_tag="\n📥 Draft: <b>D</b>")
        self.assertEqual(len(posted), 1)
        r = replies[-1]
        for bit in ("Vaseline Lotion", "₹199", "MRP ₹850", "77% off", "Discount Filter 50%+", "pass",
                    "Image Card", "dk-21"):
            self.assertIn(bit, r)

    def test_draft_gets_one_reply_not_two(self):
        _, replies = self.run_case({}, {}, 40, source_tag="\n📥 Draft: <b>D</b>")
        self.assertEqual(len(replies), 1)            # "Checking..." wala extra message nahi
        _, replies = self.run_case({}, {}, 40)       # DM mein pehle jaisa
        self.assertEqual(len(replies), 2)


if __name__ == "__main__":
    unittest.main()

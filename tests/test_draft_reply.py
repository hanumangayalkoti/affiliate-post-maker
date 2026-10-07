"""
Draft / DM reply mein HAR baar task, Destination channel aur Amazon tag dikhe.

    python -m unittest tests/test_draft_reply.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import engine  # noqa: E402


def make_task(**cfg):
    base = {"name": "Amazon Task", "tag": "dealskoti-21", "channel": "-100500",
            "channel_title": "DealsKoti Loot", "channel_username": "dealskotiloot",
            "allow_amazon": True, "allow_other": True}
    base.update(cfg)
    return {"id": 1, "user_id": 7, "cfg": base}


class DraftReplyTest(unittest.TestCase):
    def run_case(self, text, task, left=10, post_status=("posted", "ok")):
        replies = []

        async def notify(t, **kw):
            replies.append(t)
            return None

        async def amazon_urls(urls):
            return [u for u in urls if "amazon" in u]

        async def post_other(*a, **k):
            return post_status

        engine.get_amazon_urls_deep = amazon_urls
        engine.setup_problems = lambda cfg, lang: []
        engine.posts_left_today = lambda uid, tid=None: left
        engine.limits = lambda uid: {"daily": 500}
        engine.post_other = post_other
        msg = types.SimpleNamespace(caption=None, text=text, entities=[], caption_entities=[],
                                    photo=None, document=None, video=None, animation=None,
                                    video_note=None)
        asyncio.run(engine.process_and_post(None, 7, msg, notify, task, "hi",
                                            source_tag="\n📥 Draft: <b>My Draft</b>"))
        return replies

    def assert_where(self, reply, amazon=True):
        self.assertIn("@dealskotiloot", reply)        # Destination channel
        self.assertIn("Amazon Task", reply)           # task ka naam
        if amazon:
            self.assertIn("dealskoti-21", reply)      # Amazon link → tag
            self.assertNotIn("Non-Amazon link", reply)
        else:
            self.assertIn("Non-Amazon link", reply)   # Non-Amazon → saaf likha
            self.assertNotIn("dealskoti-21", reply)   # tag NAHI (lagta hi nahi)

    def test_amazon_skip_reply_has_channel_and_tag(self):
        replies = self.run_case("https://www.amazon.in/dp/B0X", make_task(allow_amazon=False))
        self.assertIn("Skip", replies[-1])
        self.assert_where(replies[-1], amazon=True)

    def test_non_amazon_skip_reply_says_non_amazon(self):
        replies = self.run_case("Flipkart https://fkrt.it/x", make_task(allow_other=False))
        self.assertIn("Skip", replies[-1])
        self.assert_where(replies[-1], amazon=False)

    def test_limit_reply_has_channel(self):
        replies = self.run_case("hello", make_task(), left=0)
        self.assert_where(replies[-1], amazon=False)

    def test_non_amazon_posted_reply_says_non_amazon(self):
        replies = self.run_case("Flipkart deal https://fkrt.it/x", make_task())
        self.assertIn("Post ho gaya", replies[-1])
        self.assert_where(replies[-1], amazon=False)

    def test_failed_and_duplicate_replies_have_channel(self):
        for status in (("error", "chat not found"), ("duplicate", "2h")):
            replies = self.run_case("Some deal text", make_task(), post_status=status)
            self.assert_where(replies[-1], amazon=False)

    def test_footer_added_once(self):
        replies = self.run_case("Flipkart deal", make_task())
        self.assertEqual(replies[-1].count("Non-Amazon link"), 1)

if __name__ == "__main__":
    unittest.main()

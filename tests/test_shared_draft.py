"""
Ek Draft channel — kai accounts. Asli Telegram / database ke bina.

    python -m unittest tests/test_shared_draft.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import main  # noqa: E402
import task_ui  # noqa: E402

DRAFT = -1001000000001


def task(tid, uid, kind):
    return {"id": tid, "user_id": uid, "paused": False,
            "cfg": {"name": f"T{tid}", "source_channel": str(DRAFT), "kind": kind}}


class SharedDraftTest(unittest.TestCase):
    def setUp(self):
        self.posted = []

        async def fake_post(context, owner, msg, notify, t, lang, source_tag=""):
            self.posted.append((owner, t["id"]))

        main.process_and_post = fake_post
        main.is_blocked = lambda uid: uid == 999
        main.is_active = lambda uid: uid != 555
        main.get_lang = lambda uid: "hi"
        main.chat_matches = lambda chat, ident: str(ident) == str(DRAFT)
        task_ui.running_ids = lambda uid: {1, 2, 3, 4}

        async def nothing(*a, **k):
            return None
        main.dm_user = nothing

    def run_post(self, tasks):
        main.find_tasks_by_source = lambda cid, uname="": tasks
        chat = types.SimpleNamespace(id=DRAFT, username="", title="Draft")
        msg = types.SimpleNamespace(chat=chat, text="deal", caption=None)
        update = types.SimpleNamespace(channel_post=msg)
        main.is_own_message = lambda m, bot_id: False
        ctx = types.SimpleNamespace(bot=types.SimpleNamespace(id=1))
        asyncio.run(main.handle_channel_post(update, ctx))
        return self.posted

    def test_two_accounts_share_one_draft_both_post(self):
        # Account A: Amazon wala task, Account B: Non-Amazon wala task — same Draft
        posted = self.run_post([task(1, 111, "amazon"), task(2, 222, "other")])
        self.assertEqual(sorted(posted), [(111, 1), (222, 2)])

    def test_same_account_two_tasks_still_work(self):
        posted = self.run_post([task(1, 111, "amazon"), task(3, 111, "other")])
        self.assertEqual(sorted(posted), [(111, 1), (111, 3)])

    def test_expired_or_blocked_account_does_not_stop_others(self):
        posted = self.run_post([task(1, 555, "a"), task(2, 999, "b"), task(4, 222, "c")])
        self.assertEqual(posted, [(222, 4)])        # sirf chalu account ki post

    def test_set_draft_no_longer_blocked_by_other_account(self):
        async def ok_channel(bot, ident, uid, need_post, lang):
            return types.SimpleNamespace(id=DRAFT, title="Draft", username=""), None

        async def nothing(*a, **k):
            return None
        saved = {}
        task_ui.verify_channel = ok_channel
        task_ui.get_task = lambda tid, uid: {"id": tid, "user_id": uid, "cfg": {"channel": "-100777"}}
        task_ui.save_task = lambda uid, tid, cfg: saved.update(cfg) or True
        task_ui.notify_task_event = nothing
        ok, text = asyncio.run(task_ui.set_task_channel(None, 222, 2, DRAFT, "src", "hi"))
        self.assertTrue(ok, text)
        self.assertEqual(saved["source_channel"], str(DRAFT))


if __name__ == "__main__":
    unittest.main()

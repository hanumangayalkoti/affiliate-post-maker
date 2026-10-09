"""
Admin panel: /user list, broadcast audience + users chunna, broadcast run + recall.

    python -m unittest tests/test_admin_panel.py -v
"""
import asyncio
import os
import sys
import types
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import admin       # noqa: E402
import broadcasts  # noqa: E402

NOW = datetime.utcnow()
USERS = [
    {"user_id": 101, "username": "vashu", "first_name": "Vashu", "expires_at": NOW + timedelta(days=4),
     "tier": "pro", "is_trial": True, "blocked": False},
    {"user_id": 102, "username": "", "first_name": "Amit", "expires_at": NOW + timedelta(days=20),
     "tier": "premium", "is_trial": False, "blocked": False},
    {"user_id": 103, "username": "", "first_name": "Ravi", "expires_at": NOW - timedelta(days=1),
     "tier": "basic", "is_trial": False, "blocked": False},
]


class PanelTest(unittest.TestCase):
    def setUp(self):
        self._o = (admin.list_users_page, admin.list_user_ids, admin.is_active)
        admin.list_users_page = lambda seg, off, lim=10: (USERS, len(USERS))
        admin.list_user_ids = lambda seg: [u["user_id"] for u in USERS]
        admin.is_active = lambda uid, u=None: (u or {}).get("expires_at", NOW) > NOW

    def tearDown(self):
        admin.list_users_page, admin.list_user_ids, admin.is_active = self._o

    def test_list_shows_plan_trial_days(self):
        text, kb = admin.users_page("all", 0)
        self.assertIn("Pro (🎁 Trial) · 4 din", text)
        self.assertIn("Premium · 20 din", text)
        self.assertIn("⌛ Khatam", text)
        datas = [b.callback_data for r in kb.inline_keyboard for b in r]
        self.assertIn("adm:v:101:all:0", datas)
        self.assertIn("adm:s", datas)                       # ID se dhoondo

    def test_audience_buttons(self):
        datas = [b.callback_data for r in admin.bc_audience_kb().inline_keyboard for b in r]
        for seg in ("all", "paid_or_trial", "active", "trial", "expired", "en", "hi"):
            self.assertIn(f"adm:bcgo:{seg}", datas)
        self.assertIn("adm:bsel:0", datas)

    def test_select_users_page(self):
        ctx = types.SimpleNamespace(user_data={"bc_sel": [102]})
        text, kb = admin.bc_select_page(ctx, 0)
        self.assertIn("Chune hue: <b>1</b>", text)
        labels = [b.text for r in kb.inline_keyboard for b in r]
        self.assertIn("✅2", labels)
        self.assertIn("✅ Bhejo (1)", labels)


class BroadcastRunTest(unittest.TestCase):
    def test_run_counts_and_recall(self):
        saved, marked = [], []
        o = (broadcasts._create, broadcasts._save_msgs, broadcasts._finish, broadcasts._msgs,
             broadcasts._mark_recalled)
        broadcasts._create = lambda *a: 7
        broadcasts._save_msgs = lambda bc, rows: saved.extend(rows)
        broadcasts._finish = lambda *a: None
        broadcasts._msgs = lambda bc: list(saved)
        broadcasts._mark_recalled = lambda bc: marked.append(bc)

        class Bot:
            deleted = []

            async def copy_message(self, chat_id, from_chat_id, message_id):
                if chat_id == 3:
                    from telegram.error import Forbidden
                    raise Forbidden("blocked")
                return types.SimpleNamespace(message_id=100 + chat_id)

            async def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))

            async def send_message(self, *a, **k):
                pass

        class Msg:
            last = ""

            async def edit_text(self, t, **k):
                Msg.last = t
        try:
            bot = Bot()
            res = asyncio.run(broadcasts.run(bot, 1, 1, 9, [1, 2, 3], "all", status_msg=Msg(),
                                             mark_bot_blocked=lambda u: None))
            self.assertEqual(res, (7, 2, 0, 1))
            self.assertIn("Broadcast poora", Msg.last)
            self.assertEqual(asyncio.run(broadcasts.recall(bot, 7)), (2, 0))
            self.assertEqual(bot.deleted, [(1, 101), (2, 102)])
            self.assertEqual(marked, [7])
            self.assertFalse(broadcasts.is_running())
        finally:
            (broadcasts._create, broadcasts._save_msgs, broadcasts._finish, broadcasts._msgs,
             broadcasts._mark_recalled) = o


if __name__ == "__main__":
    unittest.main()

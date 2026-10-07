"""
Chat clean ke test — asli Telegram / database ke bina (nakli bot).

Chalane ka tareeka (repo ke root se):
    python -m unittest tests/test_chat_clean.py -v
"""
import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import storage  # noqa: E402
import ui  # noqa: E402


class FakeChat:
    """Ek private chat: har naya message agla number leta hai (Telegram jaisa)."""

    def __init__(self):
        self.next_id = 1
        self.alive = {}            # id -> text
        self.failing = set()       # in IDs ka delete fail hoga (purana / pehle hi gaya)
        self.bulk_fails = False

    def new(self, text):
        mid = self.next_id
        self.next_id += 1
        self.alive[mid] = text
        return mid

    def texts(self):
        return [self.alive[k] for k in sorted(self.alive)]


class ChatCleanTest(unittest.TestCase):
    def setUp(self):
        self.chat = FakeChat()
        self.ctx = types.SimpleNamespace(user_data={}, bot=object())
        self.ctx._user_id = 42
        # DB nahi — yaad sirf memory mein
        self.saved = {}
        storage.screen_state_get = lambda uid: self.saved.get(uid, {"cmd": None, "ids": [], "known": []})
        storage.screen_state_set = lambda uid, st: self.saved.__setitem__(uid, st)

        chat = self.chat

        async def bulk(bot, chat_id, ids):
            if chat.bulk_fails:
                raise RuntimeError("Bad Request: message can't be deleted")
            for i in ids:
                chat.alive.pop(i, None)
            return True

        async def one(bot, chat_id, mid):
            if mid in chat.failing:
                raise RuntimeError("Bad Request: message to delete not found")
            chat.alive.pop(mid, None)
            return True

        ui._raw_delete = bulk
        ui._raw_delete_one = one

    def run_async(self, coro):
        return asyncio.run(coro)

    async def command(self, text):
        """User command bhejta hai, bot new_screen chalata hai aur jawab deta hai (screen)."""
        mid = self.chat.new(text)
        await ui.new_screen(self.ctx, None, 1, mid, ui.command_name(text))
        reply = self.chat.new(f"response to {text}#{mid}")
        ui.track(self.ctx, types.SimpleNamespace(message_id=reply))
        await asyncio.sleep(0)            # background delete chalne do
        for _ in range(5):
            await asyncio.sleep(0)

    async def report(self, text="REPORT"):
        """Report — bot bhejta hai, track NAHI hota."""
        self.chat.new(text)

    def test_example1_repeated_start_keeps_only_last(self):
        async def go():
            for _ in range(4):
                await self.command("/start")
        self.run_async(go())
        # sirf aakhri /start + uska jawab bache
        self.assertEqual(self.chat.texts(), ["/start", "response to /start#7"])

    def test_example2_report_breaks_sequence(self):
        async def go():
            await self.command("/start")
            await self.report()
            await self.command("/start")
        self.run_async(go())
        self.assertEqual(self.chat.texts(),
                         ["/start", "response to /start#1", "REPORT", "/start", "response to /start#4"])

    def test_report_then_more_starts_only_cleans_after_report(self):
        async def go():
            await self.command("/start")
            await self.report()
            await self.command("/start")
            await self.command("/start")
        self.run_async(go())
        self.assertEqual(self.chat.texts(),
                         ["/start", "response to /start#1", "REPORT", "/start", "response to /start#6"])

    def test_different_command_breaks_sequence(self):
        async def go():
            await self.command("/start")
            await self.command("/help")
            await self.command("/start")
        self.run_async(go())
        self.assertEqual(len(self.chat.texts()), 6)       # kuch delete nahi hua

    def test_start_with_args_counts_as_start(self):
        async def go():
            await self.command("/start")
            await self.command("/start ref_ABC")
        self.run_async(go())
        self.assertEqual(self.chat.texts(), ["/start ref_ABC", "response to /start ref_ABC#3"])

    def test_spam_skipped_commands_are_cleared(self):
        async def go():
            await self.command("/start")
            for _ in range(4):                            # jawab skip hue (spam)
                ui.track_id(self.ctx, self.chat.new("/start"), "start")
            await self.command("/start")
        self.run_async(go())
        self.assertEqual(self.chat.texts(), ["/start", "response to /start#7"])

    def test_delete_errors_do_not_crash_and_others_still_go(self):
        self.chat.bulk_fails = True                       # bulk fail → ek-ek karke
        async def go():
            await self.command("/start")
            self.chat.failing.add(1)                      # pehla /start delete nahi ho sakta
            await self.command("/start")
        with self.assertLogs("ui", level="WARNING") as logs:
            self.run_async(go())
        # pehla /start (1) delete nahi ho saka — log hua; uska jawab phir bhi hata
        self.assertEqual(self.chat.texts(), ["/start", "/start", "response to /start#3"])
        self.assertTrue(any("delete nahi hua" in line for line in logs.output))

    def test_restart_remembers_screen(self):
        async def go():
            await self.command("/start")
            await asyncio.sleep(0.6)                      # screen DB mein save ho gayi
            self.ctx.user_data.clear()                    # bot restart — memory khaali
            await self.command("/start")
        self.saved = {}
        self.run_async(go())
        self.assertEqual(self.chat.texts(), ["/start", "response to /start#3"])


if __name__ == "__main__":
    unittest.main()

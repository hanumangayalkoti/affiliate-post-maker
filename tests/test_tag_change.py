"""
Amazon Affiliate Tag: button naam, report text, Change button → naya tag → confirm → replace.

    python -m unittest tests/test_tag_change.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import task_ui  # noqa: E402

PATCH = ("get_task", "save_task", "show", "notify_task_event", "task_text", "task_kb", "track")


class TagFlowTest(unittest.TestCase):
    def setUp(self):
        self._o = {n: getattr(task_ui, n) for n in PATCH}
        self.cfg = {"name": "dealskoti", "tag": "dealskoti-21"}
        self.saved, self.shown, self.replies = [], [], []
        task_ui.get_task = lambda tid, uid: {"id": 5, "user_id": 7, "paused": False, "cfg": dict(self.cfg)}

        def save(uid, tid, cfg):
            self.saved.append(cfg["tag"])
            self.cfg = dict(cfg)
            return True
        task_ui.save_task = save

        async def show(q, ctx, text, kb=None):
            self.shown.append((text, kb))
        task_ui.show = show

        async def nothing(*a, **k):
            return None
        task_ui.notify_task_event = nothing
        task_ui.task_text = lambda uid, t, lang: "TASK"
        task_ui.task_kb = lambda uid, t, lang: None
        task_ui.track = lambda *a, **k: None
        self.ctx = types.SimpleNamespace(user_data={}, bot=None)

    def tearDown(self):
        for n, f in self._o.items():
            setattr(task_ui, n, f)

    def cb(self, data):
        async def answer(*a, **k):
            pass
        q = types.SimpleNamespace(answer=answer, get_bot=lambda: None,
                                  message=types.SimpleNamespace(chat_id=7))
        asyncio.run(task_ui.handle_task_callback(q, self.ctx, 7, data, "hi")
                    if task_ui.handle_task_callback.__code__.co_argcount >= 5
                    else task_ui.handle_task_callback(q, self.ctx, 7, data))

    def send(self, text):
        async def reply_text(t, **kw):
            self.replies.append((t, kw.get("reply_markup")))
            return None
        msg = types.SimpleNamespace(text=text, reply_text=reply_text, chat_id=7, message_id=1)
        upd = types.SimpleNamespace(message=msg, effective_user=types.SimpleNamespace(id=7))
        asyncio.run(task_ui.handle_task_input(upd, self.ctx, 7, self.ctx.user_data.get("action")))

    def test_button_label_and_report(self):
        task = {"id": 5, "user_id": 7, "paused": False, "cfg": {"tag": "x-21"}}
        self.assertIn("Amazon Affiliate Tag", task_ui.tag_text(task, "hi"))
        datas = [b.text for r in task_ui.tag_kb(task, "hi").inline_keyboard for b in r]
        self.assertIn("✏️ Change Affiliate Tag", datas)

    def test_change_needs_confirmation(self):
        self.cb("t:5:tag")
        self.assertNotEqual(self.ctx.user_data.get("action"), "t_tag")      # sirf dikhata hai
        self.cb("t:5:tagchg")
        self.assertEqual(self.ctx.user_data.get("action"), "t_tag")
        self.send("newtag-21")
        self.assertEqual(self.saved, [])                                    # abhi save nahi
        self.assertIn("newtag-21", self.replies[-1][0])
        self.cb("t:5:tagok")
        self.assertEqual(self.saved, ["newtag-21"])                         # confirm ke baad replace
        self.assertIn("➜", self.shown[-1][0])

    def test_cancel_keeps_old(self):
        self.cb("t:5:tagchg")
        self.send("newtag-21")
        self.cb("t:5:tag")                                                  # Cancel
        self.cb("t:5:tagok")                                                # purana confirm ab bekaar
        self.assertEqual(self.saved, [])

    def test_first_tag_saved_directly(self):
        self.cfg = {"name": "t", "tag": ""}
        self.cb("t:5:tag")
        self.assertEqual(self.ctx.user_data.get("action"), "t_tag")         # seedha maanga
        self.send("first-21")
        self.assertEqual(self.saved, ["first-21"])


if __name__ == "__main__":
    unittest.main()

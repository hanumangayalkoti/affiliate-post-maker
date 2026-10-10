"""
⭐ Make Default: pehle info screen (Default kya hota hai), phir confirm pe hi default set.

    python -m unittest tests/test_default_info.py -v
"""
import asyncio
import os
import sys
import types
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import task_ui  # noqa: E402

PATCH = ("get_task", "show", "set_default_task", "default_task", "task_text", "task_kb", "get_lang")


class DefaultInfoTest(unittest.TestCase):
    def setUp(self):
        self._o = {n: getattr(task_ui, n) for n in PATCH}
        self.shown, self.defaults = [], []
        self.task = {"id": 5, "user_id": 7, "paused": False, "cfg": {"name": "Deals <B>"}}
        task_ui.get_task = lambda tid, uid: dict(self.task)
        task_ui.default_task = lambda uid: {"id": 3, "cfg": {"name": "Old"}}
        task_ui.set_default_task = lambda uid, tid: self.defaults.append(tid)
        task_ui.task_text = lambda uid, t, lang: "TASK"
        task_ui.task_kb = lambda uid, t, lang: None
        task_ui.get_lang = lambda uid: "hi"

        async def show(q, ctx, text, kb=None):
            self.shown.append((text, kb))
        task_ui.show = show

    def tearDown(self):
        for n, f in self._o.items():
            setattr(task_ui, n, f)

    def _press(self, data):
        async def answer(*a, **k):
            return None
        q = types.SimpleNamespace(answer=answer, data=data, message=None,
                                  from_user=types.SimpleNamespace(id=7))
        ctx = types.SimpleNamespace(user_data={}, bot=None)
        asyncio.run(task_ui.handle_task_callback(q, ctx, 7, data))

    def test_info_first_then_confirm(self):
        self._press("t:5:def")
        self.assertEqual(self.defaults, [])                 # abhi set nahi
        text, kb = self.shown[-1]
        self.assertIn("DM", text)
        self.assertIn("Old", text)
        self.assertIn("&lt;B&gt;", text)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("t:5:defok", cbs)
        self.assertIn("t:5", cbs)
        self._press("t:5:defok")
        self.assertEqual(self.defaults, [5])

    def test_english(self):
        text = task_ui.default_info_text(7, self.task, "en")
        self.assertIn("What does Default do", text)


if __name__ == "__main__":
    unittest.main()

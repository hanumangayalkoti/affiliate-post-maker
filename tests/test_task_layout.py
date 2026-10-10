"""
Task screen halki (sirf zaroori) + ⚙️ Advanced Settings + ℹ️ Ye kya hai? — dono bhasha.

    python -m unittest tests/test_task_layout.py -v
"""
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import task_ui  # noqa: E402

T = {"id": 5, "user_id": 7, "paused": False, "cfg": {"name": "d", "tag": "d-21", "min_discount": 70}}


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self._o = (task_ui.limits, task_ui.default_task)
        task_ui.limits = lambda uid, u=None: {"card": True, "tasks": 2, "daily": 1500}
        task_ui.default_task = lambda uid: None

    def tearDown(self):
        task_ui.limits, task_ui.default_task = self._o

    def datas(self, kb):
        return [b.callback_data for r in kb.inline_keyboard for b in r]

    def test_main_only_essentials(self):
        d = self.datas(task_ui.task_kb(7, T, "hi"))
        for must in ("t:5:tag", "t:5:src", "t:5:dest", "t:5:filt", "t:5:pause", "t:5:adv"):
            self.assertIn(must, d)
        for moved in ("t:5:wm", "t:5:disc", "t:5:bold", "t:5:badge", "t:5:card"):
            self.assertNotIn(moved, d)

    def test_advanced_has_rest_and_info(self):
        d = self.datas(task_ui.adv_kb(7, T, "en"))
        for must in ("t:5:wm", "t:5:disc", "t:5:bold", "t:5:badge", "t:5:card", "t:5:advinfo", "t:5"):
            self.assertIn(must, d)

    def test_advanced_back_buttons(self):
        self.assertEqual(self.datas(task_ui.toggle_kb(T, "bold", True, "hi"))[-2], "t:5:adv")
        self.assertEqual(self.datas(task_ui.disc_kb(T, "hi"))[-2], "t:5:adv")
        self.assertEqual(self.datas(task_ui.tag_kb(T, "hi"))[-2], "t:5")      # main wali screen

    def test_language(self):
        self.assertIn("Ye kya hai?", task_ui.adv_info_text(T, "hi"))
        self.assertIn("What is this?", task_ui.adv_info_text(T, "en"))
        self.assertIn("Kaun Si Posts", [b.text for r in task_ui.task_kb(7, T, "hi").inline_keyboard for b in r][3])


if __name__ == "__main__":
    unittest.main()

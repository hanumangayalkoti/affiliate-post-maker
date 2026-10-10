"""
🧭 Setup Wizard: kab dikhe, har step ki info, sample post sirf poora setup hone pe,
aur Amazon tag sirf Amazon deal ke liye zaroori.

    python -m unittest tests/test_wizard.py -v
"""
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import wizard  # noqa: E402
import engine  # noqa: E402

REAL_SETUP_PROBLEMS = engine.setup_problems     # dusre tests ise globally badal dete hain

FULL = {"source_channel": "-1001", "channel": "-1002", "tag": "me-21",
        "source_title": "Draft", "channel_title": "Deals"}


def _buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


class WizardTest(unittest.TestCase):
    def setUp(self):
        self._o = {n: getattr(wizard, n) for n in ("list_tasks", "user_stats", "is_admin", "get_lang")}
        wizard.is_admin = lambda uid: False
        wizard.get_lang = lambda uid: self.lang
        self.lang = "hi"
        self.tasks, self.total = [], 0
        wizard.list_tasks = lambda uid: self.tasks
        wizard.user_stats = lambda uid, since: {"total": self.total}

    def tearDown(self):
        for n, f in self._o.items():
            setattr(wizard, n, f)

    def test_needs_setup(self):
        self.assertTrue(wizard.needs_setup(1))
        self.tasks = [{"cfg": dict(FULL, tag="")}]
        self.assertTrue(wizard.needs_setup(1))               # tag baaki
        self.tasks = [{"cfg": dict(FULL)}]
        self.assertFalse(wizard.needs_setup(1))              # task poora
        self.tasks = [{"cfg": dict(FULL, tag="", allow_amazon=False)}]
        self.assertFalse(wizard.needs_setup(1))              # sirf Non-Amazon → tag nahi chahiye
        self.tasks, self.total = [], 3
        self.assertFalse(wizard.needs_setup(1))              # forwarding chal chuki

    def test_first_open_step(self):
        self.assertEqual(wizard.first_open_step({}), 0)
        self.assertEqual(wizard.first_open_step({"source_channel": "-1"}), 1)
        self.assertEqual(wizard.first_open_step(dict(FULL, tag="")), 2)
        self.assertEqual(wizard.first_open_step(FULL), 3)

    def test_step_info_both_languages(self):
        task = {"id": 9, "cfg": {}}
        for lang, words in (("hi", ("Ye kya hai", "Zaroori", "naya private channel", "admin")),
                            ("en", ("What is it", "Required", "new private channel", "admin"))):
            self.lang = lang
            text, kb = wizard.step_screen(1, task, 0)
            for w in words:
                self.assertIn(w, text)
            self.assertIn("wz:set:src", _buttons(kb))
            text, _ = wizard.step_screen(1, task, 2)
            self.assertIn("commission", text)

    def test_done_step_sample_only_when_complete(self):
        text, kb = wizard.step_screen(1, {"id": 9, "cfg": {"source_channel": "-1"}}, 3)
        self.assertNotIn("wz:sample", _buttons(kb))
        self.assertIn("wz:go:1", _buttons(kb))
        self.assertIn("wz:go:2", _buttons(kb))
        text, kb = wizard.step_screen(1, {"id": 9, "cfg": dict(FULL)}, 3)
        self.assertIn("wz:sample", _buttons(kb))
        self.assertIn("t:9:adv", _buttons(kb))
        self.assertIn("Settings", text)

    def test_tag_only_needed_for_amazon(self):
        cfg = {"source_channel": "-1", "channel": "-2", "tag": ""}
        self.assertTrue(any("tag" in p.lower() for p in REAL_SETUP_PROBLEMS(cfg, "en", amazon=True)))
        self.assertFalse(any("tag" in p.lower() for p in REAL_SETUP_PROBLEMS(cfg, "en", amazon=False)))


if __name__ == "__main__":
    unittest.main()

"""
Admin ko naye user / trial ki detail wali khabar.

    python -m unittest tests/test_admin_alerts.py -v
"""
import os
import sys
import types
import unittest
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import alerts    # noqa: E402
import referral  # noqa: E402


class AdminAlertTest(unittest.TestCase):
    def setUp(self):
        self._o = (alerts.get_user, alerts.user_counts, referral.referrer_of)
        alerts.get_user = lambda uid: ({"first_name": "Vashu", "username": "", "joined_at": datetime(2026, 10, 8, 3, 10)}
                                       if uid == 89 else {"first_name": "Ref", "username": "r"})
        alerts.user_counts = lambda: {"total": 152, "new_today": 7}

    def tearDown(self):
        alerts.get_user, alerts.user_counts, referral.referrer_of = self._o

    def test_card_has_details(self):
        referral.referrer_of = lambda uid: 12
        tg = types.SimpleNamespace(first_name="Vashu", username="", language_code="hi")
        text = "\n".join(alerts.user_card(89, tg))
        for bit in ("Vashu", "<code>89</code>", "08 Oct 2026, 08:40 AM IST", "hi", "nahi hai", "<code>12</code>"):
            self.assertIn(bit, text)

    def test_no_referrer(self):
        referral.referrer_of = lambda uid: None
        self.assertIn("seedha aaya", "\n".join(alerts.user_card(89)))

    def test_users_line(self):
        self.assertIn("152", alerts.users_line())


if __name__ == "__main__":
    unittest.main()

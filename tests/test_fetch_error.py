import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import amazon_api  # noqa: E402


class FetchErrorTest(unittest.TestCase):
    def setUp(self):
        amazon_api._fetch_errors.clear()
        amazon_api.RETRY_WAIT_SECONDS = 0

    def run_call(self, results):
        calls = []

        async def fake_once(asins):
            calls.append(list(asins))
            return results[min(len(calls) - 1, len(results) - 1)]

        with mock.patch.object(amazon_api, "_get_items_once", fake_once):
            out = asyncio.run(amazon_api._call_get_items(["B0H187KBK2"]))
        return out, calls

    def test_busy_retries_then_succeeds(self):
        ok = {"itemsResult": {"items": [{"asin": "B0H187KBK2",
                                         "itemInfo": {"title": {"displayValue": "X"}}}]}}
        out, calls = self.run_call([(None, "busy", True), (ok, "", False)])
        self.assertEqual(len(calls), 2)
        self.assertIn("B0H187KBK2", out)
        self.assertEqual(amazon_api.fetch_error("B0H187KBK2"), "")

    def test_busy_twice_records_reason(self):
        out, calls = self.run_call([(None, "busy", True)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(out, {})
        self.assertEqual(amazon_api.fetch_error("b0h187kbk2"), "busy")

    def test_no_retry_for_non_retryable(self):
        out, calls = self.run_call([(None, "auth", False)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(amazon_api.fetch_error("B0H187KBK2"), "auth")

    def test_item_error_mapped(self):
        data = {"errors": [{"code": "ItemNotAccessible",
                            "message": "The ItemId B0H187KBK2 is not accessible through the API."}]}
        out, calls = self.run_call([(data, "", False)])
        self.assertEqual(out, {})
        self.assertEqual(amazon_api.fetch_error("B0H187KBK2"), "not_accessible")

    def test_empty_response(self):
        out, _ = self.run_call([({}, "", False)])
        self.assertEqual(amazon_api.fetch_error("B0H187KBK2"), "empty")


if __name__ == "__main__":
    unittest.main()

"""
Non-Amazon duplicate: product number (Myntra / Flipkart / Shopsy) → caption bina link.

    python -m unittest tests/test_store_dup.py -v
"""
import asyncio
import os
import sys
import unittest

from aiohttp import web

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import engine     # noqa: E402
import store_ids  # noqa: E402

REAL_POST_OTHER = engine.post_other      # doosre test files ise nakli se badal dete hain

MYNTRA = "https://www.myntra.com/tshirts/imsa-moda/imsa-moda-typography-tshirt/31076617/buy"
FK = "https://www.flipkart.com/x/p/itmb07d67f995271?pid=MOBH4DQFG8NKFRDY&affid=abc"
SHOPSY = "https://shopsy.in/fan/p/itm33ad052f2dea9?pid=MOBH4DQFG8NKFRDY"


class ProductKeyTest(unittest.TestCase):
    def test_direct_links(self):
        self.assertEqual(store_ids.store_product_key(MYNTRA), "myntra:31076617")
        self.assertEqual(store_ids.store_product_key(FK), "fk:MOBH4DQFG8NKFRDY")
        self.assertEqual(store_ids.store_product_key(SHOPSY), "fk:MOBH4DQFG8NKFRDY")   # Shopsy = Flipkart pid

    def test_tracking_link_with_target_inside(self):
        import urllib.parse
        u = "https://track.example.in/r?url=" + urllib.parse.quote(MYNTRA, safe="")
        self.assertEqual(store_ids.store_product_key(u), "myntra:31076617")

    def test_unknown_link(self):
        self.assertEqual(store_ids.store_product_key("https://myntr.it/vXjm1nP"), "")
        self.assertEqual(store_ids.store_product_key("https://example.com/p/123"), "")


class ShortLinkFollowTest(unittest.TestCase):
    """Nakli 'myntr.it' — redirect aur HTML ke andar ka link."""

    def run_server(self, coro):
        async def main():
            app = web.Application()

            async def redirect(req):
                raise web.HTTPFound(MYNTRA)

            async def html(req):
                esc_fk = FK.replace("/", "\\/")          # JSON jaisa: https:\/\/...
                return web.Response(text='<html><script>var u="' + esc_fk + '";</script></html>',
                                    content_type="text/html")

            async def jshop(req):
                return web.Response(text='<script>window.location.href="/r1"</script>', content_type="text/html")

            app.router.add_get("/r1", redirect)
            app.router.add_get("/h1", html)
            app.router.add_get("/j1", jshop)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            orig = store_ids._is_public_host
            store_ids._is_public_host = lambda h: True
            store_ids._cache.clear()
            try:
                return await coro(f"http://127.0.0.1:{port}")
            finally:
                store_ids._is_public_host = orig
                await runner.cleanup()
        return asyncio.run(main())

    def test_redirect(self):
        self.assertEqual(self.run_server(lambda b: store_ids.find_product_key(b + "/r1")), "myntra:31076617")

    def test_link_inside_html(self):
        self.assertEqual(self.run_server(lambda b: store_ids.find_product_key(b + "/h1")), "fk:MOBH4DQFG8NKFRDY")

    def test_js_redirect_then_redirect(self):
        self.assertEqual(self.run_server(lambda b: store_ids.find_product_key(b + "/j1")), "myntra:31076617")

    def test_dead_link_is_empty(self):
        async def go(b):
            return await store_ids.find_product_key("http://127.0.0.1:1/nope")
        store_ids._cache.clear()
        orig = store_ids._is_public_host
        store_ids._is_public_host = lambda h: True
        try:
            self.assertEqual(asyncio.run(go("")), "")
        finally:
            store_ids._is_public_host = orig


class DupKeyPriorityTest(unittest.TestCase):
    def keys(self, text, products):
        async def fake(urls, limit=5):
            return products
        orig = engine.find_product_keys
        engine.find_product_keys = fake
        try:
            return asyncio.run(engine._other_dup_keys({"text": text, "entities": []}))
        finally:
            engine.find_product_keys = orig

    def test_product_first(self):
        check, extra, why = self.keys("Stylish Printed T-shirt Flat 86% OFF https://myntr.it/vXjm1nP",
                                      ["myntra:31076617"])
        self.assertEqual((check, why), (["p:myntra:31076617"], "product"))
        self.assertEqual(extra, ["n:stylish printed t shirt flat 86 off"])

    def test_caption_when_link_fails(self):
        a = self.keys("Stylish Printed T-shirt Flat 86% OFF https://myntr.it/vXjm1nP", [])
        b = self.keys("Stylish Printed T-shirt Flat 86% OFF https://myntr.it/Kv4ttD9", [])
        self.assertEqual(a[0], b[0])                    # alag link, same key
        self.assertEqual(a[2], "caption")

    def test_short_caption_keeps_link(self):
        a = self.keys("Loot deal 🔥 https://myntr.it/aaa", [])
        b = self.keys("Loot deal 🔥 https://myntr.it/bbb", [])
        self.assertNotEqual(a[0], b[0])                 # chhota caption — alag deals alag

    def test_amazon_links_unchanged(self):
        check, extra, why = self.keys("Amazon sale live now for everyone https://www.amazon.in/deals?x=1", ["x"])
        self.assertEqual((extra, why), ([], ""))
        self.assertTrue(check[0].startswith("c:"))


class PostOtherDuplicateTest(unittest.TestCase):
    def test_reply_says_same_product(self):
        async def fake(urls, limit=5):
            return ["myntra:31076617"]
        claimed = []

        def claim(uid, tid, *keys):
            claimed.append(keys)
            return (False, "2h") if keys and keys[0].startswith("p:") else (True, None)
        o1, o2 = engine.find_product_keys, engine.claim_posted
        engine.find_product_keys, engine.claim_posted = fake, claim
        try:
            task = {"id": 1, "cfg": {"channel": "-100", "dup_check": True}}
            st, detail = asyncio.run(REAL_POST_OTHER(None, 7, task,
                                                       {"text": "Stylish T-shirt 86% OFF now https://myntr.it/x",
                                                        "entities": []}, "hi"))
        finally:
            engine.find_product_keys, engine.claim_posted = o1, o2
        self.assertEqual(st, "duplicate")
        self.assertIn("same product", detail)
        self.assertEqual(claimed, [("p:myntra:31076617",)])


class MultiProductTest(unittest.TestCase):
    def run_post(self, taken):
        async def fake(urls, limit=5):
            return ["myntra:1", "myntra:2", "myntra:3"]
        claimed = []

        def claim(uid, tid, *keys):
            if keys and keys[0] in taken:
                return False, "3h"
            claimed.append(keys)
            return True, None
        o = (engine.find_product_keys, engine.claim_posted, engine.post_amazon_product)
        engine.find_product_keys, engine.claim_posted = fake, claim
        try:
            task = {"id": 1, "cfg": {"channel": "-100", "dup_check": True}}
            # context=None → asli send fail hoga; hume sirf duplicate faisla dekhna hai
            st, detail = asyncio.run(REAL_POST_OTHER(None, 7, task,
                                                     {"text": "Three deals in one post today https://a https://b https://c",
                                                      "entities": []}, "hi"))
        finally:
            engine.find_product_keys, engine.claim_posted, _ = o
        return st, detail, claimed

    def test_all_old_is_duplicate(self):
        st, detail, _ = self.run_post({"p:myntra:1", "p:myntra:2", "p:myntra:3"})
        self.assertEqual(st, "duplicate")
        self.assertIn("same product", detail)

    def test_one_new_still_posts(self):
        st, _, claimed = self.run_post({"p:myntra:1", "p:myntra:2"})
        self.assertNotEqual(st, "duplicate")
        self.assertIn(("p:myntra:3",), claimed)


if __name__ == "__main__":
    unittest.main()

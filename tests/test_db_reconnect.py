"""
Der se khaali pada (mara hua) DB connection — use karne se pehle pakad ke naya lena.

    python -m unittest tests/test_db_reconnect.py -v
"""
import os
import sys
import unittest

os.environ.setdefault("BOT_TOKEN", "1:x")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import storage  # noqa: E402


class FakeConn:
    def __init__(self, dead=False):
        self.dead, self.closed = dead, 0

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql):
                if conn.dead:
                    raise Exception("server closed the connection unexpectedly")
        return Cur()

    def rollback(self):
        pass


class FakePool:
    def __init__(self, conns):
        self.conns, self.closed_out = list(conns), []

    def getconn(self):
        return self.conns.pop(0)

    def putconn(self, conn, close=False):
        if close:
            self.closed_out.append(conn)


class HealthyConnTest(unittest.TestCase):
    def setUp(self):
        storage._last_used.clear()

    def test_dead_idle_conn_replaced(self):
        dead, good = FakeConn(dead=True), FakeConn()
        pool = FakePool([dead, good])
        self.assertIs(storage._healthy_conn(pool), good)
        self.assertEqual(pool.closed_out, [dead])

    def test_recently_used_conn_not_pinged(self):
        dead_but_fresh = FakeConn(dead=True)
        storage._last_used[id(dead_but_fresh)] = 10**12        # abhi-abhi use hua
        pool = FakePool([dead_but_fresh])
        self.assertIs(storage._healthy_conn(pool), dead_but_fresh)

    def test_closed_conn_skipped(self):
        closed, good = FakeConn(), FakeConn()
        closed.closed = 1
        pool = FakePool([closed, good])
        self.assertIs(storage._healthy_conn(pool), good)


if __name__ == "__main__":
    unittest.main()

"""
database.py — duplicate check, park queue, Amazon product cache, post stats
aur price-watch. Sab kuch user ke hisaab se alag.
"""
import re
import json
import logging
from datetime import datetime, timedelta

from storage import get_db, utcnow, local_day_start_utc

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_HOURS = 24
CLEANUP_AFTER_HOURS    = 72

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE    = re.compile(r"\s+")


# =============================================================================
# DUPLICATE DETECTION (per user)
# =============================================================================
def _normalise(text: str) -> str:
    if not text:
        return ""
    low = text.lower().strip()
    low = _PUNCT_RE.sub(" ", low)
    low = _WS_RE.sub(" ", low).strip()
    return low[:300]


def _human_gap(then: datetime) -> str:
    mins = int((utcnow() - then).total_seconds() // 60)
    if mins < 1:
        return "abhi abhi"
    if mins < 60:
        return f"{mins} minute pehle"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ghante pehle"
    return f"{hours // 24} din pehle"


def is_duplicate(user_id: int, text: str):
    """(True, "2 ghante pehle") agar is user ne ye pehle post kiya hai."""
    key = _normalise(text)
    if not key:
        return False, None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT posted_at FROM seen_posts WHERE user_id = %s AND title_key = %s",
                    (user_id, key),
                )
                row = cur.fetchone()
        if row and utcnow() - row[0] < timedelta(hours=DUPLICATE_WINDOW_HOURS):
            return True, _human_gap(row[0])
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
    return False, None


def mark_posted(user_id: int, text: str):
    key = _normalise(text)
    if not key:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seen_posts (user_id, title_key, posted_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, title_key) DO UPDATE SET posted_at = EXCLUDED.posted_at
                    """,
                    (user_id, key, utcnow()),
                )
    except Exception as e:
        logger.error(f"Mark posted error: {e}")


def cleanup_old_entries():
    try:
        cutoff = utcnow() - timedelta(hours=CLEANUP_AFTER_HOURS)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM seen_posts WHERE posted_at < %s", (cutoff,))
    except Exception as e:
        logger.error(f"Cleanup error: {e}")


# =============================================================================
# POST QUEUE (park mode, per user)
# =============================================================================
def queue_add_amazon(user_id: int, asin: str) -> bool:
    if not asin:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM post_queue WHERE user_id = %s AND kind = 'amazon' AND asin = %s",
                    (user_id, asin),
                )
                if cur.fetchone():
                    return False
                cur.execute(
                    "INSERT INTO post_queue (user_id, kind, asin, payload, arrived_at) "
                    "VALUES (%s, 'amazon', %s, NULL, %s)",
                    (user_id, asin, utcnow()),
                )
        return True
    except Exception as e:
        logger.error(f"Queue add (amazon) error: {e}")
        return False


def queue_add_other(user_id: int, payload: dict) -> bool:
    payload = payload or {}
    if not (payload.get("text") or "").strip() \
            and not payload.get("photo_file_id") \
            and not payload.get("media_file_id"):
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO post_queue (user_id, kind, asin, payload, arrived_at) "
                    "VALUES (%s, 'other', NULL, %s, %s)",
                    (user_id, json.dumps(payload, ensure_ascii=False), utcnow()),
                )
        return True
    except Exception as e:
        logger.error(f"Queue add (other) error: {e}")
        return False


def queue_fetch_all(user_id: int) -> list:
    out = []
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, kind, asin, payload, arrived_at, tries FROM post_queue "
                    "WHERE user_id = %s ORDER BY arrived_at ASC, id ASC",
                    (user_id,),
                )
                rows = cur.fetchall()
        for r in rows:
            payload = {}
            if r[3]:
                try:
                    payload = json.loads(r[3])
                except Exception:
                    payload = {}
            out.append({"id": r[0], "kind": r[1], "asin": r[2], "payload": payload,
                        "arrived_at": r[4], "tries": r[5]})
    except Exception as e:
        logger.error(f"Queue fetch error: {e}")
    return out


def queue_users() -> list:
    """Jin users ki queue mein kuch pada hai."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT user_id FROM post_queue WHERE user_id IS NOT NULL")
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Queue users error: {e}")
        return []


def queue_delete(item_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_queue WHERE id = %s", (item_id,))
    except Exception as e:
        logger.error(f"Queue delete error: {e}")


def queue_bump_tries(item_id: int) -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE post_queue SET tries = tries + 1 WHERE id = %s RETURNING tries",
                            (item_id,))
                row = cur.fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Queue bump error: {e}")
        return 0


def queue_purge_old(user_id: int, max_age_hours: int = 4) -> int:
    try:
        cutoff = utcnow() - timedelta(hours=max_age_hours)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM post_queue WHERE user_id = %s AND arrived_at < %s RETURNING id",
                    (user_id, cutoff),
                )
                return len(cur.fetchall())
    except Exception as e:
        logger.error(f"Queue purge error: {e}")
        return 0


def queue_clear(user_id: int) -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_queue WHERE user_id = %s RETURNING id", (user_id,))
                return len(cur.fetchall())
    except Exception as e:
        logger.error(f"Queue clear error: {e}")
        return 0


def queue_counts(user_id: int) -> tuple:
    """(amazon_count, other_count)"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT kind, COUNT(*) FROM post_queue WHERE user_id = %s GROUP BY kind",
                            (user_id,))
                rows = dict(cur.fetchall())
        return rows.get("amazon", 0), rows.get("other", 0)
    except Exception as e:
        logger.error(f"Queue count error: {e}")
        return 0, 0


# =============================================================================
# PRODUCT CACHE (sab users ke liye common)
# =============================================================================
def cache_get(asins: list, max_age_minutes: float) -> dict:
    """{asin: (product, fetched_at)} — sirf utne fresh jitna max_age."""
    if not asins:
        return {}
    cutoff = utcnow() - timedelta(minutes=max_age_minutes)
    out = {}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT asin, data, fetched_at FROM product_cache "
                    "WHERE asin = ANY(%s) AND fetched_at >= %s",
                    (list(asins), cutoff),
                )
                for asin, data, fetched_at in cur.fetchall():
                    prod = data if isinstance(data, dict) else json.loads(data)
                    out[asin] = (prod, fetched_at)
    except Exception as e:
        logger.error(f"Cache get error: {e}")
    return out


def cache_put_many(products: dict):
    if not products:
        return
    now = utcnow()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                for asin, prod in products.items():
                    cur.execute(
                        """
                        INSERT INTO product_cache (asin, data, fetched_at) VALUES (%s, %s, %s)
                        ON CONFLICT (asin) DO UPDATE
                        SET data = EXCLUDED.data, fetched_at = EXCLUDED.fetched_at
                        """,
                        (asin, json.dumps(prod, ensure_ascii=False), now),
                    )
    except Exception as e:
        logger.error(f"Cache put error: {e}")


def cache_cleanup(keep_days: int) -> int:
    try:
        cutoff = utcnow() - timedelta(days=keep_days)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM product_cache WHERE fetched_at < %s RETURNING asin", (cutoff,))
                return len(cur.fetchall())
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")
        return 0


def cache_count() -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM product_cache")
                return cur.fetchone()[0]
    except Exception:
        return 0


# =============================================================================
# POST STATS
# =============================================================================
def log_post(user_id: int, kind: str, asin: str = "", title: str = ""):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO post_log (user_id, kind, asin, title) VALUES (%s, %s, %s, %s)",
                    (user_id, kind, asin or None, (title or "")[:200]),
                )
                cur.execute("UPDATE users SET total_posts = total_posts + 1 WHERE user_id = %s",
                            (user_id,))
    except Exception as e:
        logger.error(f"Log post error: {e}")


def posts_today(user_id: int) -> int:
    try:
        start = local_day_start_utc()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM post_log WHERE user_id = %s AND posted_at >= %s",
                            (user_id, start))
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"posts_today error: {e}")
        return 0


def user_stats(user_id: int, day_start: datetime) -> dict:
    """Aaj / 7 din / 30 din / total + recent titles."""
    out = {"today": 0, "week": 0, "month": 0, "total": 0,
           "amazon_month": 0, "other_month": 0, "recent": []}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE posted_at >= %s),
                      COUNT(*) FILTER (WHERE posted_at >= %s),
                      COUNT(*) FILTER (WHERE posted_at >= %s),
                      COUNT(*) FILTER (WHERE posted_at >= %s AND kind = 'amazon'),
                      COUNT(*) FILTER (WHERE posted_at >= %s AND kind <> 'amazon')
                    FROM post_log WHERE user_id = %s
                    """,
                    (day_start, day_start - timedelta(days=6), day_start - timedelta(days=29),
                     day_start - timedelta(days=29), day_start - timedelta(days=29), user_id),
                )
                r = cur.fetchone()
                out.update(today=r[0], week=r[1], month=r[2], amazon_month=r[3], other_month=r[4])
                cur.execute("SELECT total_posts FROM users WHERE user_id = %s", (user_id,))
                t = cur.fetchone()
                out["total"] = t[0] if t else 0
                cur.execute(
                    "SELECT title, posted_at FROM post_log WHERE user_id = %s AND kind = 'amazon' "
                    "ORDER BY posted_at DESC LIMIT 5",
                    (user_id,),
                )
                out["recent"] = cur.fetchall()
    except Exception as e:
        logger.error(f"user_stats error: {e}")
    return out


def global_post_stats(day_start: datetime) -> dict:
    out = {"today": 0, "month": 0, "active_posters_today": 0}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FILTER (WHERE posted_at >= %s),
                           COUNT(*) FILTER (WHERE posted_at >= %s),
                           COUNT(DISTINCT user_id) FILTER (WHERE posted_at >= %s)
                    FROM post_log
                    """,
                    (day_start, day_start - timedelta(days=29), day_start),
                )
                r = cur.fetchone()
                out.update(today=r[0], month=r[1], active_posters_today=r[2])
    except Exception as e:
        logger.error(f"global stats error: {e}")
    return out


def post_log_cleanup(keep_days: int = 120):
    try:
        cutoff = utcnow() - timedelta(days=keep_days)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM post_log WHERE posted_at < %s", (cutoff,))
    except Exception as e:
        logger.error(f"post_log cleanup error: {e}")


# =============================================================================
# PRICE WATCH
# =============================================================================
def watch_add(user_id: int, asin: str, title: str, price: float, target=None) -> str:
    """'added' / 'updated' / 'error'"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM price_watch WHERE user_id = %s AND asin = %s",
                            (user_id, asin))
                exists = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO price_watch (user_id, asin, title, base_price, last_price, target_price)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, asin) DO UPDATE
                    SET title = EXCLUDED.title, base_price = EXCLUDED.base_price,
                        last_price = EXCLUDED.last_price, target_price = EXCLUDED.target_price,
                        created_at = NOW()
                    """,
                    (user_id, asin, (title or "")[:200], price, price, target),
                )
        return "updated" if exists else "added"
    except Exception as e:
        logger.error(f"watch_add error: {e}")
        return "error"


def watch_count(user_id: int) -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM price_watch WHERE user_id = %s", (user_id,))
                return cur.fetchone()[0]
    except Exception:
        return 0


def watch_list(user_id: int) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, asin, title, base_price, last_price, target_price, created_at "
                    "FROM price_watch WHERE user_id = %s ORDER BY created_at DESC",
                    (user_id,),
                )
                cols = ["id", "asin", "title", "base_price", "last_price", "target_price", "created_at"]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"watch_list error: {e}")
        return []


def watch_get(watch_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, user_id, asin, title, base_price, last_price, target_price "
                    "FROM price_watch WHERE id = %s",
                    (watch_id,),
                )
                r = cur.fetchone()
        if not r:
            return None
        return dict(zip(["id", "user_id", "asin", "title", "base_price", "last_price",
                         "target_price"], r))
    except Exception as e:
        logger.error(f"watch_get error: {e}")
        return None


def watch_delete(user_id: int, watch_id: int) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM price_watch WHERE id = %s AND user_id = %s RETURNING id",
                            (watch_id, user_id))
                return bool(cur.fetchone())
    except Exception as e:
        logger.error(f"watch_delete error: {e}")
        return False


def watch_all_for_active(admin_ids: list) -> list:
    """Saare watches jinke user ka plan chalu hai (admin hamesha)."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT w.id, w.user_id, w.asin, w.title, w.base_price, w.last_price, w.target_price
                    FROM price_watch w JOIN users u ON u.user_id = w.user_id
                    WHERE u.blocked = FALSE
                      AND (u.expires_at > NOW() OR u.user_id = ANY(%s))
                    """,
                    (list(admin_ids),),
                )
                cols = ["id", "user_id", "asin", "title", "base_price", "last_price", "target_price"]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"watch_all error: {e}")
        return []


def watch_update_price(watch_id: int, price: float, alerted: bool):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if alerted:
                    cur.execute("UPDATE price_watch SET last_price = %s, last_alert_at = NOW() "
                                "WHERE id = %s", (price, watch_id))
                else:
                    cur.execute("UPDATE price_watch SET last_price = %s WHERE id = %s",
                                (price, watch_id))
    except Exception as e:
        logger.error(f"watch_update error: {e}")


def watch_expire(days: int) -> list:
    """Purane watches hata do — (user_id, title) list wapas."""
    try:
        cutoff = utcnow() - timedelta(days=days)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM price_watch WHERE created_at < %s RETURNING user_id, title",
                            (cutoff,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"watch_expire error: {e}")
        return []

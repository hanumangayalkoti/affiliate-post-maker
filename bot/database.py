"""
database.py — duplicate check (har task ka alag), Amazon product cache,
post stats aur daily message ginti.
"""
import re
import json
import hashlib
import logging
from datetime import datetime, timedelta

from storage import get_db, utcnow, local_day_start_utc

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_HOURS = 24
CLEANUP_AFTER_HOURS    = 72

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE    = re.compile(r"\s+")


# =============================================================================
# DUPLICATE DETECTION (har task ka alag)
# =============================================================================
def normalise_caption(text: str) -> str:
    """
    Duplicate key — emoji, punctuation, case, extra space hata ke. Link RAKHTE hain:
    'Loot deal 🔥 <link>' jaisi do alag deals ka text same hota hai, sirf link alag.
    Lamba text pura gina jaata hai (hash), taaki aakhir ka link bhi count ho.
    """
    if not text:
        return ""
    low = _PUNCT_RE.sub(" ", text.lower())
    low = _WS_RE.sub(" ", low).strip()
    if len(low) <= 200:
        return low
    return low[:120] + "#" + hashlib.sha1(low.encode("utf-8")).hexdigest()[:20]


def _key(task_id: int, key: str) -> str:
    return f"t{task_id}:{key}"


def _human_gap(then: datetime) -> str:
    mins = int((utcnow() - then).total_seconds() // 60)
    if mins < 1:
        return "0m"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def is_duplicate(user_id: int, task_id: int, key: str):
    """(True, "2h") agar is task mein ye pehle (24 ghante mein) post hua hai."""
    if not key:
        return False, None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT posted_at FROM seen_posts WHERE user_id = %s AND title_key = %s",
                    (user_id, _key(task_id, key)),
                )
                row = cur.fetchone()
        if row and utcnow() - row[0] < timedelta(hours=DUPLICATE_WINDOW_HOURS):
            return True, _human_gap(row[0])
    except Exception as e:
        logger.error(f"Duplicate check error: {e}")
    return False, None


def mark_posted(user_id: int, task_id: int, *keys):
    keys = [k for k in keys if k]
    if not keys:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                for k in keys:
                    cur.execute(
                        """
                        INSERT INTO seen_posts (user_id, title_key, posted_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id, title_key) DO UPDATE SET posted_at = EXCLUDED.posted_at
                        """,
                        (user_id, _key(task_id, k), utcnow()),
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
def log_post(user_id: int, task_id: int, kind: str, asin: str = "", title: str = ""):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO post_log (user_id, task_id, kind, asin, title) VALUES (%s, %s, %s, %s, %s)",
                    (user_id, task_id, kind, asin or None, (title or "")[:200]),
                )
                cur.execute("UPDATE users SET total_posts = total_posts + 1 WHERE user_id = %s",
                            (user_id,))
    except Exception as e:
        logger.error(f"Log post error: {e}")


def posts_today(user_id: int, task_id: int = None) -> int:
    """Aaj (raat 12 baje IST se) kitni post. task_id diya to sirf us task ki —
    daily limit har task ki alag hai."""
    try:
        start = local_day_start_utc()
        with get_db() as conn:
            with conn.cursor() as cur:
                if task_id is None:
                    cur.execute("SELECT COUNT(*) FROM post_log WHERE user_id = %s AND posted_at >= %s",
                                (user_id, start))
                else:
                    cur.execute("SELECT COUNT(*) FROM post_log WHERE user_id = %s AND task_id = %s "
                                "AND posted_at >= %s", (user_id, task_id, start))
                return cur.fetchone()[0]
    except Exception as e:
        logger.error(f"posts_today error: {e}")
        return 0


def user_stats(user_id: int, day_start: datetime) -> dict:
    """Aaj / 7 din / 30 din / total + har task ki aaj ki ginti."""
    out = {"today": 0, "week": 0, "month": 0, "total": 0,
           "amazon_month": 0, "other_month": 0, "by_task": {}}
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
                cur.execute("SELECT task_id, COUNT(*) FROM post_log WHERE user_id = %s "
                            "AND posted_at >= %s GROUP BY task_id", (user_id, day_start))
                out["by_task"] = {tid: n for tid, n in cur.fetchall()}
    except Exception as e:
        logger.error(f"user_stats error: {e}")
    return out


def day_report_rows(start: datetime, end: datetime) -> dict:
    """Ek din ki posts: {user_id: {task_id: (amazon, other)}} — din ke end wali report ke liye."""
    out: dict = {}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, task_id,
                           COUNT(*) FILTER (WHERE kind = 'amazon'),
                           COUNT(*) FILTER (WHERE kind <> 'amazon')
                    FROM post_log WHERE posted_at >= %s AND posted_at < %s
                    GROUP BY user_id, task_id
                    """,
                    (start, end),
                )
                for uid, tid, amz, other in cur.fetchall():
                    out.setdefault(uid, {})[tid] = (amz, other)
    except Exception as e:
        logger.error(f"day_report_rows error: {e}")
    return out


def last_amazon_asins(user_id: int, limit: int = 5) -> list:
    """User ki latest Amazon posts ke ASIN — card preview ke liye."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT asin FROM post_log WHERE user_id = %s AND kind = 'amazon' "
                    "AND asin IS NOT NULL ORDER BY posted_at DESC LIMIT %s",
                    (user_id, limit),
                )
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"last_amazon_asins error: {e}")
        return []


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

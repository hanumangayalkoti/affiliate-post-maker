"""
storage.py — PostgreSQL connection pool, saare tables, purane data ka
migration, aur TASKS (har task = 1 Draft + 1 Destination + apni saari settings).

Har setting DB mein save hoti hai — bot crash / restart / redeploy pe kuch
nahi khota. Memory mein sirf cache hai (har write pe update hota hai).
"""
import os
import json
import copy
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2 import pool as pg_pool

from card import DEFAULT_CARD, clean_card, clean_watermark

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

# ── TIME ─────────────────────────────────────────────────────────────────
# DB mein saare time UTC (bina timezone) — DB session bhi UTC pe set hai, taaki
# Python ka time aur SQL ka NOW() hamesha match karein. Dikhate waqt IST.
TZ_NAME = os.getenv("TZ_NAME", "Asia/Kolkata")
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))


def utcnow() -> datetime:
    """DB ke liye abhi ka time (UTC, naive)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt):
    """DB ka UTC time → IST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def local_day_start_utc() -> datetime:
    """Aaj raat 12 baje (IST) — UTC naive mein, DB queries ke liye."""
    loc = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


# Amazon post ke saare fields — har ek ka apna on/off
DEFAULT_AMZ_FIELDS = {
    "title":    True,
    "image":    True,
    "mrp":      True,
    "price":    True,
    "savings":  True,
    "discount": True,
    "rating":   True,
    "reviews":  True,
    "link":     True,
    "deal":     False,
    "stock":    False,
    "seller":   False,
    "brand":    False,
    "rank":     False,
    "features": False,
}

AMZ_FIELD_ORDER = [
    "title", "deal", "mrp", "price", "savings", "discount",
    "rating", "reviews", "stock", "brand", "seller", "rank", "features",
]

# Naye task ki default settings
DEFAULT_TASK = {
    "name":             "",
    "tag":              "",      # Amazon affiliate tag (jaise abc-21)
    "channel":          "",      # Destination (numeric id)
    "channel_title":    "",
    "channel_username": "",
    "source_channel":   "",      # Draft channel (numeric id)
    "source_title":     "",
    "source_username":  "",
    "silent":           True,
    "allow_amazon":     True,    # Amazon posts jaayengi?
    "allow_other":      True,    # Non-Amazon posts jaayengi?
    "dup_check":        True,    # 24 ghante mein same post dobara nahi
    "strip_promo":      True,    # doosre channel ke @username / Telegram links hatao
    "bold_links":       True,    # post ke links bold dikhein
    "min_discount":     0,       # Amazon Discount Filter — isse kam % wali deal skip (0 = OFF)
    "search_links":     False,
    "amazon_badge":     True,    # Amazon post ki photo pe chhota 'available at amazon' logo
    "amz_detailed":     True,
    "amz_fields":       DEFAULT_AMZ_FIELDS,
    "header":           {"enabled": False, "text": ""},
    "footer":           {"enabled": False, "text": ""},
    "watermark":        {"enabled": False, "text": "", "position": "bottom_right",
                         "size": "s", "color": "white"},
    "card":             DEFAULT_CARD,
    "buttons": {
        "btn1": {"label": "Join Channel", "url": "", "enabled": False, "style": ""},
        "btn2": {"label": "More Deals",   "url": "", "enabled": False, "style": ""},
        "buy":  {"label": "⚡ Buy Now",     "enabled": False, "style": ""},
        "cart": {"label": "🛒 Add to Cart", "enabled": False, "style": ""},
    },
}


# =============================================================================
# CONNECTION POOL
# =============================================================================
_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL environment variable set nahi hai!")
                _pool = pg_pool.ThreadedConnectionPool(1, 10, DATABASE_URL,
                                                       options="-c timezone=UTC")
    return _pool


@contextmanager
def get_db():
    """Pool se connection lo — commit karo, galti pe rollback, hamesha wapas do."""
    p = _get_pool()
    conn = p.getconn()
    broken = False
    try:
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        try:
            p.putconn(conn, close=broken or bool(conn.closed))
        except Exception:
            pass


# =============================================================================
# SCHEMA
# =============================================================================
def init_db():
    """Saare tables banao / naye columns jodo + purana data naye structure mein."""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ui_screens (
                    user_id BIGINT PRIMARY KEY,
                    msg_ids JSONB  NOT NULL DEFAULT '[]'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id       BIGINT PRIMARY KEY,
                    username      TEXT,
                    first_name    TEXT,
                    joined_at     TIMESTAMP NOT NULL DEFAULT NOW(),
                    expires_at    TIMESTAMP,
                    blocked       BOOLEAN   NOT NULL DEFAULT FALSE,
                    bot_blocked   BOOLEAN   NOT NULL DEFAULT FALSE,
                    remind_stage  INTEGER   NOT NULL DEFAULT 0,
                    total_posts   INTEGER   NOT NULL DEFAULT 0,
                    last_seen     TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            for col, ddl in (("tier", "TEXT"),
                             ("is_trial", "BOOLEAN NOT NULL DEFAULT FALSE"),
                             ("trial_used", "BOOLEAN NOT NULL DEFAULT FALSE"),
                             ("lang", "TEXT"),
                             ("default_task", "BIGINT")):
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {ddl}")
            cur.execute("CREATE INDEX IF NOT EXISTS users_expires_idx ON users (expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS users_username_idx ON users (LOWER(username))")

            # Purani per-user config — sirf migration ke liye padhte hain
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id BIGINT PRIMARY KEY,
                    data    JSONB NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id         BIGSERIAL PRIMARY KEY,
                    user_id    BIGINT    NOT NULL,
                    data       JSONB     NOT NULL,
                    paused     BOOLEAN   NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS tasks_user_idx ON tasks (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS tasks_source_idx ON tasks ((data->>'source_channel'))")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    user_id   BIGINT    NOT NULL,
                    title_key TEXT      NOT NULL,
                    posted_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (user_id, title_key)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS seen_posts_time_idx ON seen_posts (posted_at)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS product_cache (
                    asin       TEXT PRIMARY KEY,
                    data       JSONB     NOT NULL,
                    fetched_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS product_cache_time_idx ON product_cache (fetched_at)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS post_log (
                    id        BIGSERIAL PRIMARY KEY,
                    user_id   BIGINT    NOT NULL,
                    kind      TEXT      NOT NULL,
                    asin      TEXT,
                    title     TEXT,
                    posted_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE post_log ADD COLUMN IF NOT EXISTS task_id BIGINT")
            cur.execute("CREATE INDEX IF NOT EXISTS post_log_user_time_idx ON post_log (user_id, posted_at)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id          BIGSERIAL PRIMARY KEY,
                    user_id     BIGINT    NOT NULL,
                    provider    TEXT      NOT NULL,
                    ref         TEXT      NOT NULL,
                    receipt     TEXT,
                    amount      INTEGER   NOT NULL,
                    currency    TEXT      NOT NULL,
                    days        INTEGER   NOT NULL,
                    status      TEXT      NOT NULL DEFAULT 'pending',
                    payment_id  TEXT,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
                    paid_at     TIMESTAMP,
                    UNIQUE (provider, ref)
                )
            """)
            cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS tier TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS payments_receipt_idx ON payments (receipt)")

    _migrate_old_admin_config()
    _migrate_user_config_to_tasks()
    logger.info("Database tables ready.")


def _migrate_old_admin_config():
    """Sabse purana bot sirf admin ka tha — uski config bot_config mein thi."""
    if not ADMIN_ID:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM user_config WHERE user_id = %s", (ADMIN_ID,))
                if cur.fetchone():
                    return
                cur.execute("SELECT value FROM bot_config WHERE key = 'config'")
                row = cur.fetchone()
                if not row:
                    return
                try:
                    old = json.loads(row[0])
                except Exception:
                    old = {}
                old.setdefault("tag", os.getenv("PARTNER_TAG", ""))
                cur.execute("INSERT INTO user_config (user_id, data) VALUES (%s, %s) "
                            "ON CONFLICT (user_id) DO NOTHING",
                            (ADMIN_ID, json.dumps(old, ensure_ascii=False)))
    except Exception as e:
        logger.error(f"Old admin migration error: {e}")


def _migrate_user_config_to_tasks():
    """Purani per-user settings (user_config) → us user ka 'Task 1'. Ek hi baar."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.user_id, c.data FROM user_config c
                    WHERE NOT EXISTS (SELECT 1 FROM tasks t WHERE t.user_id = c.user_id)
                """)
                rows = cur.fetchall()
                for uid, data in rows:
                    old = data if isinstance(data, dict) else json.loads(data)
                    cfg = fill_task_defaults(copy.deepcopy(old))
                    cfg["name"] = cfg.get("name") or "Task 1"
                    cur.execute("INSERT INTO tasks (user_id, data) VALUES (%s, %s) RETURNING id",
                                (uid, json.dumps(cfg, ensure_ascii=False)))
                    tid = cur.fetchone()[0]
                    cur.execute("UPDATE users SET default_task = %s WHERE user_id = %s "
                                "AND default_task IS NULL", (tid, uid))
                if rows:
                    logger.info(f"{len(rows)} users ki purani settings Task 1 mein aa gayi.")
    except Exception as e:
        logger.error(f"Task migration error: {e}")


# =============================================================================
# TASK CONFIG
# =============================================================================
_KNOWN_KEYS = set(DEFAULT_TASK)


def fill_task_defaults(cfg: dict) -> dict:
    """Jo key missing hai wo default se bharo — purani config bhi chalti rahe."""
    if not isinstance(cfg, dict):
        cfg = {}
    for k, v in DEFAULT_TASK.items():
        if k in ("amz_fields", "header", "footer", "watermark", "buttons", "card"):
            continue
        cfg.setdefault(k, copy.deepcopy(v))

    fields = cfg.get("amz_fields")
    if not isinstance(fields, dict):
        fields = cfg["amz_fields"] = {}
    for k, v in DEFAULT_AMZ_FIELDS.items():
        fields.setdefault(k, v)

    for key in ("header", "footer"):
        d = cfg.get(key)
        if not isinstance(d, dict):
            d = cfg[key] = {}
        d.setdefault("enabled", False)
        d.setdefault("text", "")

    cfg["watermark"] = clean_watermark(cfg.get("watermark"))
    cfg["card"] = clean_card(cfg.get("card"))

    btns = cfg.get("buttons")
    if not isinstance(btns, dict):
        btns = cfg["buttons"] = {}
    for bk, bv in DEFAULT_TASK["buttons"].items():
        b = btns.setdefault(bk, {})
        for k, v in bv.items():
            b.setdefault(k, v)

    # Purane bot ki keys jo ab kaam ki nahi
    for k in ("park_post", "pricedrop_autopost"):
        cfg.pop(k, None)
    return cfg


def new_task_config(name: str = "") -> dict:
    cfg = fill_task_defaults(copy.deepcopy(DEFAULT_TASK))
    cfg["name"] = name
    return cfg


# ── Cache: user_id → [task, ...]. Har write pe us user ka cache hat jaata hai.
_task_cache: dict = {}
_task_lock = threading.Lock()


def _forget_tasks(user_id: int):
    with _task_lock:
        _task_cache.pop(user_id, None)


def _row_to_task(r) -> dict:
    tid, uid, data, paused, created = r
    cfg = data if isinstance(data, dict) else json.loads(data)
    return {"id": tid, "user_id": uid, "paused": bool(paused), "created_at": created,
            "cfg": fill_task_defaults(cfg)}


def list_tasks(user_id: int) -> list:
    """User ke saare tasks (purane pehle). Har task: id, user_id, paused, cfg."""
    with _task_lock:
        hit = _task_cache.get(user_id)
    if hit is not None:
        return copy.deepcopy(hit)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, user_id, data, paused, created_at FROM tasks "
                            "WHERE user_id = %s ORDER BY id", (user_id,))
                tasks = [_row_to_task(r) for r in cur.fetchall()]
        with _task_lock:
            if len(_task_cache) > 5000:
                _task_cache.clear()
            _task_cache[user_id] = copy.deepcopy(tasks)
        return tasks
    except Exception as e:
        logger.error(f"list_tasks error ({user_id}): {e}")
        return []


def get_task(task_id: int, user_id: int = None):
    """Ek task. user_id diya to sirf usi ka task milega (dusre ka nahi)."""
    if user_id is not None:
        for t in list_tasks(user_id):
            if t["id"] == task_id:
                return t
        return None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, user_id, data, paused, created_at FROM tasks WHERE id = %s",
                            (task_id,))
                r = cur.fetchone()
        return _row_to_task(r) if r else None
    except Exception as e:
        logger.error(f"get_task error ({task_id}): {e}")
        return None


def create_task(user_id: int, cfg: dict):
    """Naya task. Returns task id ya None."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO tasks (user_id, data) VALUES (%s, %s) RETURNING id",
                            (user_id, json.dumps(fill_task_defaults(cfg), ensure_ascii=False)))
                tid = cur.fetchone()[0]
        _forget_tasks(user_id)
        return tid
    except Exception as e:
        logger.error(f"create_task error ({user_id}): {e}")
        return None


def save_task(user_id: int, task_id: int, cfg: dict) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tasks SET data = %s WHERE id = %s AND user_id = %s",
                            (json.dumps(cfg, ensure_ascii=False), task_id, user_id))
                ok = cur.rowcount > 0
        _forget_tasks(user_id)
        return ok
    except Exception as e:
        logger.error(f"save_task error ({task_id}): {e}")
        _forget_tasks(user_id)
        return False


def patch_task_cfg(user_id: int, task_id: int, patch: dict) -> bool:
    """Task ki settings mein SIRF ye keys likho (baaki jaisi DB mein hain waisi).
    Poora cfg save karne se, beech mein user ne jo badla (filter, button...) wo
    purani copy se mit jaata tha."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tasks SET data = data || %s::jsonb WHERE id = %s AND user_id = %s",
                            (json.dumps(patch, ensure_ascii=False), task_id, user_id))
                ok = cur.rowcount > 0
        _forget_tasks(user_id)
        return ok
    except Exception as e:
        logger.error(f"patch_task_cfg error ({task_id}): {e}")
        _forget_tasks(user_id)
        return False


def set_task_paused(user_id: int, task_id: int, paused: bool) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE tasks SET paused = %s WHERE id = %s AND user_id = %s",
                            (paused, task_id, user_id))
                ok = cur.rowcount > 0
        _forget_tasks(user_id)
        return ok
    except Exception as e:
        logger.error(f"set_task_paused error ({task_id}): {e}")
        return False


def delete_task(user_id: int, task_id: int) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tasks WHERE id = %s AND user_id = %s", (task_id, user_id))
                ok = cur.rowcount > 0
                cur.execute("UPDATE users SET default_task = NULL WHERE user_id = %s "
                            "AND default_task = %s", (user_id, task_id))
        _forget_tasks(user_id)
        return ok
    except Exception as e:
        logger.error(f"delete_task error ({task_id}): {e}")
        return False


def _find_tasks(field: str, chat_id: int, username: str = "") -> list:
    keys = [str(chat_id)]
    if username:
        keys.append("@" + username.lower())
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT id, user_id, data, paused, created_at FROM tasks "
                            f"WHERE LOWER(data->>'{field}') = ANY(%s) ORDER BY id", (keys,))
                return [_row_to_task(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Task lookup error ({field}): {e}")
        return []


def find_tasks_by_source(chat_id: int, username: str = "") -> list:
    return _find_tasks("source_channel", chat_id, username)


def find_tasks_by_dest(chat_id: int, username: str = "") -> list:
    return _find_tasks("channel", chat_id, username)


# =============================================================================
# CHAT CLEAN — screen ke message IDs (restart / redeploy ke baad bhi yaad)
# =============================================================================
def screen_state_get(user_id: int) -> dict:
    """Chat clean ki yaad: {"cmd": "start", "ids": [...], "known": [...]}.
    Purani format (sirf IDs ki list) bhi padh lete hain — usme command ka naam
    nahi hota, to us baar kuch delete nahi hota (safe)."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT msg_ids FROM ui_screens WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        data = row[0] if row else None
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list):
            ids = [int(x) for x in data if str(x).isdigit()]
            return {"cmd": None, "ids": ids, "known": ids}
        if isinstance(data, dict):
            clean = lambda v: [int(x) for x in (v or []) if str(x).isdigit()]   # noqa: E731
            return {"cmd": data.get("cmd") or None, "ids": clean(data.get("ids")),
                    "known": clean(data.get("known"))}
    except Exception as e:
        logger.error(f"screen_state_get error: {e}")
    return {"cmd": None, "ids": [], "known": []}


def screen_state_set(user_id: int, state: dict):
    try:
        data = {"cmd": state.get("cmd"),
                "ids": [int(x) for x in state.get("ids") or []][-80:],
                "known": [int(x) for x in state.get("known") or []][-300:]}
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ui_screens (user_id, msg_ids) VALUES (%s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET msg_ids = EXCLUDED.msg_ids""",
                    (user_id, json.dumps(data)),
                )
    except Exception as e:
        logger.error(f"screen_state_set error: {e}")

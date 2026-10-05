"""
storage.py — PostgreSQL connection pool, saare tables, purane single-admin
data ka migration, aur har user ki apni config (settings).
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

from card import DEFAULT_CARD, clean_card

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
    """DB ka UTC time → user ka local time (IST)."""
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
    "deal":     False,   # Lightning deal / deal badge + kab tak
    "stock":    False,   # Kitna stock bacha
    "seller":   False,   # Kaun bech raha hai
    "brand":    False,   # Brand ka naam
    "rank":     False,   # Best seller rank
    "features": False,   # 2 key features
}

AMZ_FIELD_ORDER = [
    "title", "deal", "mrp", "price", "savings", "discount",
    "rating", "reviews", "stock", "brand", "seller", "rank", "features",
]

# Naye user ki default settings. Header/watermark khali — har user apna daalega.
DEFAULT_CONFIG = {
    "tag":                "",      # user ka Amazon affiliate tag (jaise abc-21)
    "channel":            "",      # post channel (numeric id ya @username)
    "channel_title":      "",
    "source_channel":     "",      # draft channel
    "source_title":       "",
    "silent":             True,
    "park_post":          False,
    "search_links":       False,   # Amazon search/deals page wale link post karein?
    "pricedrop_autopost": False,   # price gire to channel pe khud post?
    "amz_detailed":       True,
    "amz_fields":         DEFAULT_AMZ_FIELDS,
    "header":             {"enabled": False, "text": ""},
    "footer":             {"enabled": False, "text": ""},
    "watermark":          {"enabled": False, "text": "", "position": "bottom_right"},
    "card":               DEFAULT_CARD,    # image card (card.py)
    "buttons": {
        "btn1": {"label": "Join Channel", "url": "", "enabled": False},
        "btn2": {"label": "More Deals",   "url": "", "enabled": False},
        "buy":  {"label": "⚡ Buy Now",     "enabled": False},
        "cart": {"label": "🛒 Add to Cart", "enabled": False},
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
    """Saare tables banao + purana data naye structure mein le aao."""
    with get_db() as conn:
        with conn.cursor() as cur:
            # ── Purane tables (pehle se the) ─────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS post_queue (
                    id         SERIAL PRIMARY KEY,
                    kind       TEXT      NOT NULL,
                    asin       TEXT,
                    payload    TEXT,
                    arrived_at TIMESTAMP NOT NULL,
                    tries      INTEGER   NOT NULL DEFAULT 0
                )
            """)
            cur.execute("ALTER TABLE post_queue ADD COLUMN IF NOT EXISTS user_id BIGINT")
            cur.execute("CREATE INDEX IF NOT EXISTS post_queue_arrived_idx ON post_queue (arrived_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS post_queue_user_idx ON post_queue (user_id)")

            # ── Users + subscription ─────────────────────────────────────
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
            cur.execute("CREATE INDEX IF NOT EXISTS users_expires_idx ON users (expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS users_username_idx ON users (LOWER(username))")

            # ── Har user ki settings ─────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id BIGINT PRIMARY KEY,
                    data    JSONB NOT NULL
                )
            """)

            # ── Duplicate check — har user ka alag ───────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    user_id   BIGINT    NOT NULL,
                    title_key TEXT      NOT NULL,
                    posted_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (user_id, title_key)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS seen_posts_time_idx ON seen_posts (posted_at)")

            # ── Amazon product cache — sab users share karte hain ────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS product_cache (
                    asin       TEXT PRIMARY KEY,
                    data       JSONB     NOT NULL,
                    fetched_at TIMESTAMP NOT NULL
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS product_cache_time_idx ON product_cache (fetched_at)")

            # ── Post stats ───────────────────────────────────────────────
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
            cur.execute("CREATE INDEX IF NOT EXISTS post_log_user_time_idx ON post_log (user_id, posted_at)")

            # ── Payments ─────────────────────────────────────────────────
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
            cur.execute("CREATE INDEX IF NOT EXISTS payments_receipt_idx ON payments (receipt)")

            # ── Price drop alerts ────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_watch (
                    id            BIGSERIAL PRIMARY KEY,
                    user_id       BIGINT    NOT NULL,
                    asin          TEXT      NOT NULL,
                    title         TEXT,
                    base_price    NUMERIC   NOT NULL,
                    last_price    NUMERIC   NOT NULL,
                    target_price  NUMERIC,
                    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
                    last_alert_at TIMESTAMP,
                    UNIQUE (user_id, asin)
                )
            """)

    _migrate_old_admin_data()
    logger.info("Database tables ready.")


def _migrate_old_admin_data():
    """
    Purana bot sirf admin ke liye tha — uski config `bot_config` mein thi aur
    queue mein user_id nahi tha. Ye sab admin ke naam kar do (ek hi baar).
    """
    if not ADMIN_ID:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE post_queue SET user_id = %s WHERE user_id IS NULL", (ADMIN_ID,))

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
                cur.execute(
                    "INSERT INTO user_config (user_id, data) VALUES (%s, %s) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (ADMIN_ID, json.dumps(old, ensure_ascii=False)),
                )
                logger.info("Purani admin config naye user_config mein copy ho gayi.")
    except Exception as e:
        logger.error(f"Migration error: {e}")


# =============================================================================
# PER-USER CONFIG
# =============================================================================
def _fill_defaults(cfg: dict) -> dict:
    """Jo key missing hai wo default se bhar do — purani config bhi chalti rahe."""
    for k, v in DEFAULT_CONFIG.items():
        if k in ("amz_fields", "header", "footer", "watermark", "buttons", "card"):
            continue
        cfg.setdefault(k, copy.deepcopy(v))

    fields = cfg.setdefault("amz_fields", {})
    for k, v in DEFAULT_AMZ_FIELDS.items():
        fields.setdefault(k, v)

    for key in ("header", "footer", "watermark"):
        d = cfg.setdefault(key, {})
        if not isinstance(d, dict):
            d = cfg[key] = {}
        d.setdefault("enabled", False)
        d.setdefault("text", "")
    cfg["watermark"].setdefault("position", "bottom_right")

    cfg["card"] = clean_card(cfg.get("card"))

    btns = cfg.setdefault("buttons", {})
    for bk, bv in DEFAULT_CONFIG["buttons"].items():
        b = btns.setdefault(bk, {})
        for k, v in bv.items():
            b.setdefault(k, v)
    return cfg


# Config cache — har message pe DB na jaana pade. Saari writes save_config se
# hoti hain, wahi cache bhi update karta hai. Asli data hamesha DB mein hai,
# isliye restart / redeploy pe kuch nahi khota.
_cfg_cache: dict = {}
_cfg_lock = threading.Lock()


def load_config(user_id: int) -> dict:
    """User ki settings lao. Pehli baar hai to default."""
    with _cfg_lock:
        hit = _cfg_cache.get(user_id)
    if hit is not None:
        return copy.deepcopy(hit)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM user_config WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
        if row:
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            cfg = _fill_defaults(data)
            with _cfg_lock:
                _cfg_cache[user_id] = copy.deepcopy(cfg)
            return cfg
        cfg = _fill_defaults(copy.deepcopy(DEFAULT_CONFIG))
        if user_id == ADMIN_ID:
            cfg["tag"] = os.getenv("PARTNER_TAG", "")
        with _cfg_lock:
            _cfg_cache[user_id] = copy.deepcopy(cfg)   # naya user — default
        return cfg
    except Exception as e:
        # DB fail — default do par cache mat karo, agli baar DB se try hoga
        logger.error(f"Config load error ({user_id}): {e}")
        cfg = _fill_defaults(copy.deepcopy(DEFAULT_CONFIG))
        if user_id == ADMIN_ID:
            cfg["tag"] = os.getenv("PARTNER_TAG", "")
        return cfg


def save_config(user_id: int, config: dict) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_config (user_id, data) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data
                    """,
                    (user_id, json.dumps(config, ensure_ascii=False)),
                )
        with _cfg_lock:
            _cfg_cache[user_id] = _fill_defaults(copy.deepcopy(config))
        return True
    except Exception as e:
        with _cfg_lock:
            _cfg_cache.pop(user_id, None)
        logger.error(f"Config save error ({user_id}): {e}")
        return False


def find_users_by_source(chat_id: int, username: str = "") -> list:
    """Is channel ko kisne draft channel banaya hai."""
    keys = [str(chat_id)]
    if username:
        keys.append("@" + username.lower())
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM user_config "
                    "WHERE LOWER(data->>'source_channel') = ANY(%s)",
                    (keys,),
                )
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Source lookup error: {e}")
        return []


def find_users_by_post_channel(chat_id: int, username: str = "") -> list:
    keys = [str(chat_id)]
    if username:
        keys.append("@" + username.lower())
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id FROM user_config "
                    "WHERE LOWER(data->>'channel') = ANY(%s)",
                    (keys,),
                )
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Channel lookup error: {e}")
        return []

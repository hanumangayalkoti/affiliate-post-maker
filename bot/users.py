"""
users.py — user record, plan (tier + expiry), trial, language, default task,
admin ke manual din, block, payments ka record aur activation.
"""
import os
import time
import logging
import threading
from datetime import datetime, timedelta

from storage import get_db, utcnow, local_day_start_utc
from tiers import (
    TIERS, ADMIN_LIMITS, TRIAL_DAYS, TRIAL_TIER, PLAN_DAYS, midnight_ceil, new_expiry,
)

logger = logging.getLogger(__name__)


def _parse_admins() -> list:
    ids = []
    raw = os.getenv("ADMIN_ID", "") + "," + os.getenv("ADMIN_IDS", "")
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            v = int(part)
            if v and v not in ids:
                ids.append(v)
    return ids


ADMIN_IDS = _parse_admins()
OWNER_ID  = ADMIN_IDS[0] if ADMIN_IDS else 0

_USER_COLS = ["user_id", "username", "first_name", "joined_at", "expires_at",
              "blocked", "bot_blocked", "remind_stage", "total_posts", "last_seen",
              "tier", "is_trial", "trial_used", "lang", "default_task", "setup_seen"]


def is_admin(uid) -> bool:
    return bool(uid) and uid in ADMIN_IDS


# get_user ka chhota cache — ek message pe 4-5 baar same user DB se na aaye.
# Har write ke baad us user ka cache hata dete hain; TTL sirf safety ke liye.
_USER_TTL = 20
_user_cache: dict = {}
_user_lock = threading.Lock()


def _forget(user_id):
    with _user_lock:
        _user_cache.pop(user_id, None)


def _exec(sql: str, params: tuple, user_id: int = None) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
        if user_id is not None:
            _forget(user_id)
        return True
    except Exception as e:
        logger.error(f"users sql error: {e}")
        return False


# =============================================================================
# USER RECORD
# =============================================================================
def upsert_user(user_id: int, username: str = "", first_name: str = "") -> bool:
    """User ko register/update karo. True agar bilkul naya user hai."""
    _forget(user_id)
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (user_id, username, first_name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                    SET username = EXCLUDED.username, first_name = EXCLUDED.first_name,
                        last_seen = NOW(), bot_blocked = FALSE
                    RETURNING (xmax = 0)
                    """,
                    (user_id, username or None, (first_name or "")[:64]),
                )
                row = cur.fetchone()
        return bool(row and row[0])
    except Exception as e:
        logger.error(f"upsert_user error: {e}")
        return False


def get_user(user_id: int):
    now = time.monotonic()
    with _user_lock:
        hit = _user_cache.get(user_id)
    if hit and now - hit[0] < _USER_TTL:
        return dict(hit[1])
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(_USER_COLS)} FROM users WHERE user_id = %s",
                            (user_id,))
                r = cur.fetchone()
        if not r:
            return None
        u = dict(zip(_USER_COLS, r))
        with _user_lock:
            if len(_user_cache) > 5000:
                _user_cache.clear()
            _user_cache[user_id] = (now, u)
        return dict(u)
    except Exception as e:
        logger.error(f"get_user error: {e}")
        return None


def find_user(query: str):
    """ID ya @username se user dhoondo."""
    q = (query or "").strip()
    if not q:
        return None
    if q.lstrip("-").isdigit():
        return get_user(int(q))
    q = q.lstrip("@").lower()
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(_USER_COLS)} FROM users "
                            f"WHERE LOWER(username) = %s LIMIT 1", (q,))
                r = cur.fetchone()
        return dict(zip(_USER_COLS, r)) if r else None
    except Exception as e:
        logger.error(f"find_user error: {e}")
        return None


def get_lang(user_id: int) -> str:
    u = get_user(user_id) or {}
    return u.get("lang") or "hi"


def set_lang(user_id: int, lang: str) -> bool:
    return _exec("UPDATE users SET lang = %s WHERE user_id = %s",
                 ("en" if lang == "en" else "hi", user_id), user_id)


def set_default_task(user_id: int, task_id) -> bool:
    return _exec("UPDATE users SET default_task = %s WHERE user_id = %s", (task_id, user_id), user_id)


# =============================================================================
# PLAN
# =============================================================================
def is_active(user_id: int, user: dict = None) -> bool:
    """Plan chalu hai? Admin hamesha active."""
    if is_admin(user_id):
        return True
    u = user or get_user(user_id)
    if not u or u.get("blocked"):
        return False
    exp = u.get("expires_at")
    return bool(exp and exp > utcnow())


def current_tier(user_id: int, user: dict = None):
    """'basic' / 'pro' / 'premium' / None (plan nahi). Admin = 'admin'."""
    if is_admin(user_id):
        return "admin"
    u = user or get_user(user_id)
    if not is_active(user_id, u):
        return None
    t = (u or {}).get("tier")
    return t if t in TIERS else "pro"


def limits(user_id: int, user: dict = None) -> dict:
    """Is user ki limits — tasks, daily messages, card. Plan nahi to sab 0."""
    t = current_tier(user_id, user)
    if t == "admin":
        return dict(ADMIN_LIMITS, key="admin")
    if t is None:
        return {"name": "—", "emoji": "❌", "tasks": 0, "daily": 0, "card": False, "key": None}
    return dict(TIERS[t], key=t)


def is_blocked(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    u = get_user(user_id)
    return bool(u and u.get("blocked"))


def days_left(user: dict) -> float:
    exp = (user or {}).get("expires_at")
    if not exp:
        return 0
    return max(0.0, (exp - utcnow()).total_seconds() / 86400)


def start_trial(user_id: int):
    """TRIAL_DAYS (5) din Pro free — har user ko ek hi baar. Returns expiry ya None."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                exp = midnight_ceil(utcnow() + timedelta(days=TRIAL_DAYS))
                cur.execute(
                    """
                    UPDATE users SET tier = %s, is_trial = TRUE, trial_used = TRUE,
                                     expires_at = %s, remind_stage = 0
                    WHERE user_id = %s AND trial_used = FALSE
                      AND (expires_at IS NULL OR expires_at <= NOW())
                    RETURNING expires_at
                    """,
                    (TRIAL_TIER, exp, user_id),
                )
                row = cur.fetchone()
        _forget(user_id)
        return row[0] if row else None
    except Exception as e:
        logger.error(f"start_trial error: {e}")
        return None


def _apply_plan_cur(cur, user_id: int, tier: str, days: int):
    """Usi transaction mein plan lagao (tier + nayi expiry). Returns (old_tier, new_exp)."""
    _forget(user_id)
    cur.execute("SELECT expires_at, tier, is_trial FROM users WHERE user_id = %s FOR UPDATE",
                (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    cur_exp, cur_tier, is_trial = row
    exp = new_expiry(utcnow(), cur_exp, cur_tier, bool(is_trial), tier, days)
    cur.execute("UPDATE users SET tier = %s, is_trial = FALSE, expires_at = %s, remind_stage = 0 "
                "WHERE user_id = %s", (tier, exp, user_id))
    return (cur_tier if (cur_exp and cur_exp > utcnow()) else None), exp


def _add_days_cur(cur, user_id: int, days: float):
    """Admin ke manual din. Nayi expiry (raat 12 baje) ya None."""
    _forget(user_id)
    cur.execute("SELECT expires_at, tier FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    now = utcnow()
    running = bool(row[0] and row[0] > now)
    if days < 0 and not running:
        new_exp = row[0] or now
    else:
        new_exp = midnight_ceil((row[0] if running else now) + timedelta(days=days))
    tier = row[1] if row[1] in TIERS else TRIAL_TIER
    cur.execute("UPDATE users SET expires_at = %s, tier = %s, remind_stage = 0 WHERE user_id = %s",
                (new_exp, tier, user_id))
    return new_exp


def add_days(user_id: int, days: float):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                new_exp = _add_days_cur(cur, user_id, days)
        _forget(user_id)
        return new_exp
    except Exception as e:
        logger.error(f"add_days error: {e}")
        return None


def set_tier(user_id: int, tier: str) -> bool:
    """Admin — tier badlo (expiry wahi rehti hai; plan nahi hai to 30 din)."""
    if tier not in TIERS:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT expires_at FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
                row = cur.fetchone()
                if not row:
                    return False
                exp = row[0]
                if not exp or exp <= utcnow() + timedelta(minutes=1):   # khatam ya bas khatam hone wala
                    exp = midnight_ceil(utcnow() + timedelta(days=PLAN_DAYS))
                cur.execute("UPDATE users SET tier = %s, is_trial = FALSE, expires_at = %s, "
                            "remind_stage = 0 WHERE user_id = %s", (tier, exp, user_id))
        _forget(user_id)
        return True
    except Exception as e:
        logger.error(f"set_tier error: {e}")
        return False


def end_plan(user_id: int) -> bool:
    return _exec("UPDATE users SET expires_at = NOW() - INTERVAL '1 second' WHERE user_id = %s",
                 (user_id,), user_id)


def set_blocked(user_id: int, blocked: bool) -> bool:
    return _exec("UPDATE users SET blocked = %s WHERE user_id = %s", (blocked, user_id), user_id)


def mark_bot_blocked(user_id: int):
    _exec("UPDATE users SET bot_blocked = TRUE WHERE user_id = %s", (user_id,), user_id)


def mark_setup_seen(user_id: int):
    """Setup Wizard ek baar apne aap dikh chuka — dobara apne aap nahi khulega."""
    _exec("UPDATE users SET setup_seen = TRUE WHERE user_id = %s", (user_id,), user_id)


def set_remind_stage(user_id: int, stage: int):
    _exec("UPDATE users SET remind_stage = %s WHERE user_id = %s", (stage, user_id), user_id)


def users_expiring_soon(hours: int) -> list:
    """Jinka plan agle `hours` mein khatam hoga aur reminder nahi gaya."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, expires_at FROM users "
                    "WHERE blocked = FALSE AND bot_blocked = FALSE AND remind_stage < 1 "
                    "AND expires_at > NOW() AND expires_at <= NOW() + %s",
                    (timedelta(hours=hours),),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"expiring_soon error: {e}")
        return []


def users_just_expired(within_days: int = 3) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, expires_at FROM users "
                    "WHERE blocked = FALSE AND bot_blocked = FALSE AND remind_stage < 2 "
                    "AND expires_at <= NOW() AND expires_at > NOW() - %s",
                    (timedelta(days=within_days),),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"just_expired error: {e}")
        return []


_SEGMENTS = {
    "all":     "TRUE",
    "active":  "expires_at > NOW() AND is_trial = FALSE",
    "trial":   "expires_at > NOW() AND is_trial = TRUE",
    "expired": "(expires_at IS NULL OR expires_at <= NOW())",
    "blocked": "blocked = TRUE",
}


def list_user_ids(segment: str = "all") -> list:
    """Broadcast ke liye."""
    where = "bot_blocked = FALSE AND blocked = FALSE"
    if segment in ("active", "trial", "expired"):
        where += " AND " + _SEGMENTS[segment]
    elif segment == "paid_or_trial":
        where += " AND expires_at > NOW()"
    elif segment == "en":
        where += " AND lang = 'en'"
    elif segment == "hi":
        where += " AND COALESCE(lang, 'hi') <> 'en'"
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id FROM users WHERE {where} ORDER BY user_id")
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"list_user_ids error: {e}")
        return []


def list_users_page(segment: str, offset: int, limit: int = 10):
    """Admin list — (users, total)."""
    where = _SEGMENTS.get(segment, "TRUE")
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM users WHERE {where}")
                total = cur.fetchone()[0]
                cur.execute(f"SELECT {', '.join(_USER_COLS)} FROM users WHERE {where} "
                            f"ORDER BY joined_at DESC LIMIT %s OFFSET %s", (limit, offset))
                return [dict(zip(_USER_COLS, r)) for r in cur.fetchall()], total
    except Exception as e:
        logger.error(f"list_users_page error: {e}")
        return [], 0


def user_counts() -> dict:
    out = {"total": 0, "active": 0, "trial": 0, "expired": 0, "blocked": 0,
           "bot_blocked": 0, "new_today": 0, "basic": 0, "pro": 0, "premium": 0}
    try:
        today = local_day_start_utc()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*),
                      COUNT(*) FILTER (WHERE expires_at > NOW() AND is_trial = FALSE),
                      COUNT(*) FILTER (WHERE expires_at > NOW() AND is_trial = TRUE),
                      COUNT(*) FILTER (WHERE expires_at IS NULL OR expires_at <= NOW()),
                      COUNT(*) FILTER (WHERE blocked),
                      COUNT(*) FILTER (WHERE bot_blocked),
                      COUNT(*) FILTER (WHERE joined_at >= %s),
                      COUNT(*) FILTER (WHERE expires_at > NOW() AND is_trial = FALSE AND tier = 'basic'),
                      COUNT(*) FILTER (WHERE expires_at > NOW() AND is_trial = FALSE AND tier = 'pro'),
                      COUNT(*) FILTER (WHERE expires_at > NOW() AND is_trial = FALSE AND tier = 'premium')
                    FROM users
                    """,
                    (today,),
                )
                r = cur.fetchone()
        out.update(total=r[0], active=r[1], trial=r[2], expired=r[3], blocked=r[4],
                   bot_blocked=r[5], new_today=r[6], basic=r[7], pro=r[8], premium=r[9])
    except Exception as e:
        logger.error(f"user_counts error: {e}")
    return out


# =============================================================================
# PAYMENTS
# =============================================================================
def payment_create_pending(user_id: int, provider: str, ref: str, receipt: str,
                           amount: int, currency: str, days: int, tier: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO payments (user_id, provider, ref, receipt, amount, currency, days, tier) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (provider, ref) DO NOTHING",
                    (user_id, provider, ref, receipt, amount, currency, days, tier),
                )
        return True
    except Exception as e:
        logger.error(f"payment_create error: {e}")
        return False


def payment_mark_paid_razorpay(receipt: str, link_id: str, payment_id: str):
    """
    Pending Razorpay payment → paid + plan lagao, dono EK transaction mein.
    Returns (user_id, tier, days, new_expiry, amount, old_tier) ya None.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE payments SET status = 'paid', payment_id = %s, paid_at = NOW()
                    WHERE provider = 'razorpay' AND status = 'pending'
                      AND ((%s <> '' AND receipt = %s) OR (%s <> '' AND ref = %s))
                    RETURNING user_id, days, amount, tier
                    """,
                    (payment_id, receipt or "", receipt or "", link_id or "", link_id or ""),
                )
                row = cur.fetchone()
                if not row:
                    return None
                uid, days, amount, tier = row
                tier = tier if tier in TIERS else "pro"
                res = _apply_plan_cur(cur, uid, tier, days)
                if res is None:
                    raise RuntimeError(f"payment user {uid} nahi mila")
        _forget(uid)
        return uid, tier, days, res[1], amount, res[0]
    except Exception as e:
        logger.error(f"payment_mark_paid error: {e}")
        return None


def payment_record_stars(user_id: int, charge_id: str, amount: int, days: int, tier: str):
    """Stars payment + plan — ek transaction. Returns (new_exp, old_tier) ya None."""
    tier = tier if tier in TIERS else "pro"
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payments (user_id, provider, ref, receipt, amount, currency, days,
                                          status, payment_id, paid_at, tier)
                    VALUES (%s, 'stars', %s, %s, %s, 'XTR', %s, 'paid', %s, NOW(), %s)
                    ON CONFLICT (provider, ref) DO NOTHING
                    RETURNING id
                    """,
                    (user_id, charge_id, charge_id, amount, days, charge_id, tier),
                )
                if not cur.fetchone():
                    return None
                res = _apply_plan_cur(cur, user_id, tier, days)
                if res is None:
                    raise RuntimeError(f"stars user {user_id} nahi mila")
        _forget(user_id)
        return res[1], res[0]
    except Exception as e:
        logger.error(f"payment_record_stars error: {e}")
        return None


def payment_exists(provider: str, ref: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM payments WHERE provider = %s AND ref = %s", (provider, ref))
                return bool(cur.fetchone())
    except Exception:
        return False


def payment_status_by_receipt(receipt: str):
    """'pending' / 'paid' / None (hamara payment nahi) / 'error'."""
    if not receipt:
        return None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM payments WHERE provider = 'razorpay' AND receipt = %s",
                            (receipt,))
                r = cur.fetchone()
        return r[0] if r else None
    except Exception:
        return "error"


def revenue_summary(day_start: datetime) -> dict:
    out = {"inr_month": 0, "stars_month": 0, "inr_total": 0, "stars_total": 0, "paid_month": 0}
    try:
        since = day_start - timedelta(days=29)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      COALESCE(SUM(amount) FILTER (WHERE currency='INR' AND paid_at >= %s), 0),
                      COALESCE(SUM(amount) FILTER (WHERE currency='XTR' AND paid_at >= %s), 0),
                      COALESCE(SUM(amount) FILTER (WHERE currency='INR'), 0),
                      COALESCE(SUM(amount) FILTER (WHERE currency='XTR'), 0),
                      COUNT(*) FILTER (WHERE paid_at >= %s)
                    FROM payments WHERE status = 'paid'
                    """,
                    (since, since, since),
                )
                r = cur.fetchone()
        out.update(inr_month=int(r[0]) // 100, stars_month=int(r[1]),
                   inr_total=int(r[2]) // 100, stars_total=int(r[3]), paid_month=r[4])
    except Exception as e:
        logger.error(f"revenue_summary error: {e}")
    return out


def recent_payments(limit: int = 10, user_id: int = None) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT user_id, provider, amount, currency, days, paid_at, tier FROM payments "
                        "WHERE status = 'paid' AND user_id = %s ORDER BY paid_at DESC LIMIT %s",
                        (user_id, limit))
                else:
                    cur.execute(
                        "SELECT user_id, provider, amount, currency, days, paid_at, tier FROM payments "
                        "WHERE status = 'paid' ORDER BY paid_at DESC LIMIT %s", (limit,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"recent_payments error: {e}")
        return []

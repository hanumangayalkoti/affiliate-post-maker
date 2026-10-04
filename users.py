"""
users.py — user record, plan (expiry), admin ke manual din, block,
payments ka record aur activation.
"""
import os
import logging
from datetime import datetime, timedelta

from storage import get_db, utcnow, local_day_start_utc

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
              "blocked", "bot_blocked", "remind_stage", "total_posts", "last_seen"]


def is_admin(uid) -> bool:
    return bool(uid) and uid in ADMIN_IDS


# =============================================================================
# USER RECORD
# =============================================================================
def upsert_user(user_id: int, username: str = "", first_name: str = "") -> bool:
    """User ko register/update karo. True agar bilkul naya user hai."""
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
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(_USER_COLS)} FROM users WHERE user_id = %s",
                            (user_id,))
                r = cur.fetchone()
        return dict(zip(_USER_COLS, r)) if r else None
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


def is_active(user_id: int, user: dict = None) -> bool:
    """Plan chalu hai? Admin hamesha active."""
    if is_admin(user_id):
        return True
    u = user or get_user(user_id)
    if not u or u.get("blocked"):
        return False
    exp = u.get("expires_at")
    return bool(exp and exp > utcnow())


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


def _add_days_cur(cur, user_id: int, days: float):
    """Usi transaction ke andar din jodo/kaato. Nayi expiry ya None (user nahi mila)."""
    cur.execute("SELECT expires_at FROM users WHERE user_id = %s FOR UPDATE", (user_id,))
    row = cur.fetchone()
    if row is None:
        return None
    now = utcnow()
    running = bool(row[0] and row[0] > now)
    if days < 0 and not running:
        new_exp = row[0] or now          # pehle se khatam — aur kya kaatein
    else:
        new_exp = (row[0] if running else now) + timedelta(days=days)
    cur.execute("UPDATE users SET expires_at = %s, remind_stage = 0 WHERE user_id = %s",
                (new_exp, user_id))
    return new_exp


def add_days(user_id: int, days: float):
    """
    Din jodo (ya minus karke kaato). Plan chalu hai to expiry aage/peeche,
    khatam ho chuka hai to aaj se gino. Nayi expiry wapas.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                return _add_days_cur(cur, user_id, days)
    except Exception as e:
        logger.error(f"add_days error: {e}")
        return None


def end_plan(user_id: int) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET expires_at = NOW() WHERE user_id = %s", (user_id,))
        return True
    except Exception as e:
        logger.error(f"end_plan error: {e}")
        return False


def set_blocked(user_id: int, blocked: bool) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET blocked = %s WHERE user_id = %s", (blocked, user_id))
        return True
    except Exception as e:
        logger.error(f"set_blocked error: {e}")
        return False


def mark_bot_blocked(user_id: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET bot_blocked = TRUE WHERE user_id = %s", (user_id,))
    except Exception:
        pass


def set_remind_stage(user_id: int, stage: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET remind_stage = %s WHERE user_id = %s", (stage, user_id))
    except Exception as e:
        logger.error(f"set_remind_stage error: {e}")


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


def list_user_ids(segment: str = "all") -> list:
    """Broadcast ke liye — all / active / expired."""
    where = "bot_blocked = FALSE AND blocked = FALSE"
    if segment == "active":
        where += " AND expires_at > NOW()"
    elif segment == "expired":
        where += " AND (expires_at IS NULL OR expires_at <= NOW())"
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT user_id FROM users WHERE {where} ORDER BY user_id")
                return [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"list_user_ids error: {e}")
        return []


def recent_users(limit: int = 15) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(_USER_COLS)} FROM users "
                            f"ORDER BY joined_at DESC LIMIT %s", (limit,))
                return [dict(zip(_USER_COLS, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"recent_users error: {e}")
        return []


def user_counts() -> dict:
    out = {"total": 0, "active": 0, "expired": 0, "never_paid": 0, "blocked": 0,
           "bot_blocked": 0, "new_today": 0}
    try:
        today = local_day_start_utc()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*),
                      COUNT(*) FILTER (WHERE expires_at > NOW()),
                      COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at <= NOW()),
                      COUNT(*) FILTER (WHERE expires_at IS NULL),
                      COUNT(*) FILTER (WHERE blocked),
                      COUNT(*) FILTER (WHERE bot_blocked),
                      COUNT(*) FILTER (WHERE joined_at >= %s)
                    FROM users
                    """,
                    (today,),
                )
                r = cur.fetchone()
        out.update(total=r[0], active=r[1], expired=r[2], never_paid=r[3],
                   blocked=r[4], bot_blocked=r[5], new_today=r[6])
    except Exception as e:
        logger.error(f"user_counts error: {e}")
    return out


# =============================================================================
# PAYMENTS
# =============================================================================
def payment_create_pending(user_id: int, provider: str, ref: str, receipt: str,
                           amount: int, currency: str, days: int) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO payments (user_id, provider, ref, receipt, amount, currency, days) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (provider, ref) DO NOTHING",
                    (user_id, provider, ref, receipt, amount, currency, days),
                )
        return True
    except Exception as e:
        logger.error(f"payment_create error: {e}")
        return False


def payment_mark_paid_razorpay(receipt: str, link_id: str, payment_id: str):
    """
    Pending Razorpay payment ko paid karo aur din jodo — dono EK hi transaction
    mein, taaki "paid ho gaya par din nahi jude" kabhi na ho. Sirf ek baar chalta hai.
    Returns (user_id, days, new_expiry, amount) ya None (pehle se paid / mila nahi).
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE payments SET status = 'paid', payment_id = %s, paid_at = NOW()
                    WHERE provider = 'razorpay' AND status = 'pending'
                      AND ((%s <> '' AND receipt = %s) OR (%s <> '' AND ref = %s))
                    RETURNING user_id, days, amount
                    """,
                    (payment_id, receipt or "", receipt or "", link_id or "", link_id or ""),
                )
                row = cur.fetchone()
                if not row:
                    return None
                new_exp = _add_days_cur(cur, row[0], row[1])
                if new_exp is None:
                    raise RuntimeError(f"payment user {row[0]} nahi mila")
        return row[0], row[1], new_exp, row[2]
    except Exception as e:
        logger.error(f"payment_mark_paid error: {e}")
        return None


def payment_record_stars(user_id: int, charge_id: str, amount: int, days: int):
    """Stars payment + din — ek transaction. charge_id unique, dobara aaya to kuch nahi."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO payments (user_id, provider, ref, receipt, amount, currency, days,
                                          status, payment_id, paid_at)
                    VALUES (%s, 'stars', %s, %s, %s, 'XTR', %s, 'paid', %s, NOW())
                    ON CONFLICT (provider, ref) DO NOTHING
                    RETURNING id
                    """,
                    (user_id, charge_id, charge_id, amount, days, charge_id),
                )
                if not cur.fetchone():
                    return None
                new_exp = _add_days_cur(cur, user_id, days)
                if new_exp is None:
                    raise RuntimeError(f"stars user {user_id} nahi mila")
                return new_exp
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
    """'pending' / 'paid' / None (hamara payment nahi)."""
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
                    (day_start - timedelta(days=29),) * 2 + (day_start - timedelta(days=29),),
                )
                r = cur.fetchone()
        out.update(inr_month=int(r[0]) // 100, stars_month=int(r[1]),
                   inr_total=int(r[2]) // 100, stars_total=int(r[3]), paid_month=r[4])
    except Exception as e:
        logger.error(f"revenue_summary error: {e}")
    return out


def recent_payments(limit: int = 10) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, provider, amount, currency, days, paid_at FROM payments "
                    "WHERE status = 'paid' ORDER BY paid_at DESC LIMIT %s",
                    (limit,),
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"recent_payments error: {e}")
        return []

"""
tiers.py — plans (Basic / Pro / Premium), unki limits, trial aur raat 12 baje
(IST) wala hisaab. Price env se badal sakte hain.
"""
import os
from datetime import datetime, timedelta, timezone

from storage import LOCAL_TZ


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


PLAN_DAYS = _env_int("PLAN_DAYS", 30)
TRIAL_DAYS = _env_int("TRIAL_DAYS", 7)
TRIAL_TIER = "pro"

TIERS = {
    "basic": {
        "name": "Basic", "emoji": "🥉",
        "inr": _env_int("PRICE_BASIC", 50), "stars": _env_int("STARS_BASIC", 50),
        "tasks": 1, "daily": _env_int("DAILY_BASIC", 200), "card": False,
    },
    "pro": {
        "name": "Pro", "emoji": "🥈",
        "inr": _env_int("PRICE_PRO", 100), "stars": _env_int("STARS_PRO", 100),
        "tasks": 2, "daily": _env_int("DAILY_PRO", 500), "card": True,
    },
    "premium": {
        "name": "Premium", "emoji": "🥇",
        "inr": _env_int("PRICE_PREMIUM", 250), "stars": _env_int("STARS_PREMIUM", 250),
        "tasks": 5, "daily": _env_int("DAILY_PREMIUM", 1200), "card": True,
    },
}
TIER_ORDER = ["basic", "pro", "premium"]

# Admin ke liye koi limit nahi
ADMIN_LIMITS = {"name": "Admin", "emoji": "👑", "tasks": 20, "daily": 0, "card": True}


def tier_label(key: str) -> str:
    t = TIERS.get(key)
    return f"{t['emoji']} {t['name']}" if t else "—"


def utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def midnight_ceil(dt_utc_naive: datetime) -> datetime:
    """Is waqt ke baad wali pehli raat 12 baje (IST) — UTC naive mein."""
    loc = dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    mid = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    if mid < loc:
        mid += timedelta(days=1)
    return utc_naive(mid)


def per_day_value(tier: str) -> float:
    t = TIERS.get(tier)
    return (t["inr"] / PLAN_DAYS) if t else 0.0


def new_expiry(now: datetime, cur_exp, cur_tier: str, is_trial: bool, buy_tier: str,
               days: int = PLAN_DAYS) -> datetime:
    """
    Plan khareedne ke baad nayi expiry (raat 12 baje IST pe).
      • Same plan chalu  → bache din + naye din
      • Alag plan chalu  → bache din ki keemat naye plan ke din mein badal ke + naye din
      • Trial / khatam   → aaj se naye din
    """
    running = bool(cur_exp and cur_exp > now)
    if running and not is_trial and cur_tier == buy_tier:
        base = cur_exp
    elif running and not is_trial and cur_tier in TIERS:
        left_days = (cur_exp - now).total_seconds() / 86400
        credit = left_days * per_day_value(cur_tier)
        extra = credit / per_day_value(buy_tier) if per_day_value(buy_tier) else 0
        base = now + timedelta(days=extra)
    else:
        base = now
    return midnight_ceil(base + timedelta(days=days))

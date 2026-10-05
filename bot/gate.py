"""
gate.py — bot shuru karne se pehle 2 kadam:
  1. Language chunna (English / Hinglish) — DB mein save
  2. Update channel join karna (FORCE_JOIN_CHANNEL) — "✅ Maine join kar liya"
Admin pe join lagu nahi hota.

Bot ko update channel mein admin banana zaroori hai — tabhi wo member check kar
sakta hai. Check na ho paaye to user ko roka nahi jaata (admin ko khabar jaati hai).
"""
import os
import time
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

from ui import btn, tr, GREEN, BLUE, TAGLINE
from users import is_admin, ADMIN_IDS, get_user

logger = logging.getLogger(__name__)
esc = html_lib.escape

FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_LINK = os.getenv("FORCE_JOIN_LINK", "").strip()
FORCE_JOIN_NAME = os.getenv("FORCE_JOIN_NAME", "").strip()
BOT_NAME = os.getenv("BOT_NAME", "Deal Post Maker")

if FORCE_JOIN_CHANNEL and not FORCE_JOIN_CHANNEL.lstrip("-").isdigit():
    _name = FORCE_JOIN_CHANNEL.split("t.me/")[-1].strip("/").lstrip("@")
    FORCE_JOIN_CHANNEL = "@" + _name
    FORCE_JOIN_LINK = FORCE_JOIN_LINK or f"https://t.me/{_name}"

OK_CACHE_SECONDS = 30 * 60
_joined_at: dict = {}
_warned = {"at": 0.0}


def join_enabled() -> bool:
    return bool(FORCE_JOIN_CHANNEL and FORCE_JOIN_LINK)


def has_lang(uid: int) -> bool:
    return bool((get_user(uid) or {}).get("lang"))


async def _check(bot, uid: int):
    """True = member, False = nahi, None = check hi nahi ho paya."""
    try:
        m = await bot.get_chat_member(FORCE_JOIN_CHANNEL, uid)
    except Exception as e:
        logger.error(f"Force-join check fail: {e}")
        if time.time() - _warned["at"] > 6 * 3600:
            _warned["at"] = time.time()
            for aid in ADMIN_IDS:
                try:
                    await bot.send_message(
                        aid, "⚠️ <b>Force join check nahi ho raha</b>\n"
                             f"Bot ko <code>{esc(FORCE_JOIN_CHANNEL)}</code> mein admin banao. "
                             "Tab tak users bina join kiye bhi bot use kar rahe hain.\n"
                             f"<i>{esc(str(e)[:120])}</i>", parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        return None
    if m.status in ("creator", "administrator", "member"):
        return True
    if m.status == "restricted":
        return bool(getattr(m, "is_member", False))
    return False


async def is_joined(bot, uid: int, fresh: bool = False) -> bool:
    if not join_enabled() or is_admin(uid):
        return True
    if not fresh and time.time() - _joined_at.get(uid, 0) < OK_CACHE_SECONDS:
        return True
    ok = await _check(bot, uid)
    if ok is None:
        return True
    if ok:
        _joined_at[uid] = time.time()
    else:
        _joined_at.pop(uid, None)
    return ok


# =============================================================================
# SCREENS
# =============================================================================
def lang_text() -> str:
    return (
        f"👋 <b>{esc(BOT_NAME)}</b>\n"
        f"<i>{TAGLINE}</i>\n\n"
        "🌐 <b>Choose your language</b>\n"
        "🌐 <b>Apni bhasha chunein</b>"
    )


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        btn("🇬🇧 English", BLUE, callback_data="lang:en"),
        btn("🇮🇳 Hinglish", BLUE, callback_data="lang:hi"),
    ]])


def join_text(lang: str) -> str:
    name = esc(FORCE_JOIN_NAME) if FORCE_JOIN_NAME else tr(lang, "our update channel", "hamara update channel")
    return tr(
        lang,
        f"🔒 <b>Just one step!</b>\n\n"
        f"Please join <b>{name}</b> to use the bot — best deals and bot updates are posted there.\n\n"
        "1️⃣ Tap <b>📢 Join Update Channel</b>\n"
        "2️⃣ Then tap <b>✅ I've Joined</b>",
        f"🔒 <b>Bas ek kadam!</b>\n\n"
        f"Bot use karne ke liye pehle <b>{name}</b> join karein — wahan roz best deals aur "
        "bot ke updates aate hain.\n\n"
        "1️⃣ <b>📢 Join Update Channel</b> dabayein\n"
        "2️⃣ Phir <b>✅ Maine join kar liya</b> dabayein",
    )


def join_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("📢 Join Update Channel", BLUE, url=FORCE_JOIN_LINK)],
        [btn(tr(lang, "✅ I've Joined", "✅ Maine join kar liya"), GREEN, callback_data="fj_check")],
    ])


def not_joined_alert(lang: str) -> str:
    return tr(lang, "❌ You haven't joined yet. Tap 'Join Update Channel' first.",
              "❌ Aapne abhi join nahi kiya. Pehle 'Join Update Channel' dabayein.")

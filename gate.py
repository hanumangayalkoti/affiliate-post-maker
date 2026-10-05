"""
gate.py — force join. Bot use karne se pehle user ko owner ka channel join
karna padta hai (FORCE_JOIN_CHANNEL). "✅ Maine join kar liya" dabane pe
check hota hai. Admin pe ye lagu nahi hota.

Bot ko us channel mein admin banana zaroori hai — tabhi wo member check kar
sakta hai. Check na ho paaye to user ko roka nahi jaata (admin ko khabar jaati hai).
"""
import os
import time
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

from ui import btn, GREEN, BLUE
from users import is_admin, ADMIN_IDS

logger = logging.getLogger(__name__)
esc = html_lib.escape

FORCE_JOIN_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()
FORCE_JOIN_LINK = os.getenv("FORCE_JOIN_LINK", "").strip()
FORCE_JOIN_NAME = os.getenv("FORCE_JOIN_NAME", "").strip()

if FORCE_JOIN_CHANNEL and not FORCE_JOIN_CHANNEL.lstrip("-").isdigit():
    name = FORCE_JOIN_CHANNEL.split("t.me/")[-1].strip("/").lstrip("@")
    FORCE_JOIN_CHANNEL = "@" + name
    FORCE_JOIN_LINK = FORCE_JOIN_LINK or f"https://t.me/{name}"

OK_CACHE_SECONDS = 30 * 60
_joined_at: dict = {}
_warned = {"at": 0.0}


def enabled() -> bool:
    return bool(FORCE_JOIN_CHANNEL and FORCE_JOIN_LINK)


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
    if not enabled() or is_admin(uid):
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


def join_text() -> str:
    name = esc(FORCE_JOIN_NAME) if FORCE_JOIN_NAME else "hamara channel"
    return (
        "🔒 <b>Bas ek kadam!</b>\n\n"
        f"Bot use karne ke liye pehle <b>{name}</b> join karo — wahan roz best "
        "deals aur bot ke updates aate hain.\n\n"
        "1️⃣ Neeche <b>Join Channel</b> dabao\n"
        "2️⃣ Phir <b>✅ Maine join kar liya</b> dabao"
    )


def join_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("📢 Join Channel", BLUE, url=FORCE_JOIN_LINK)],
        [btn("✅ Maine join kar liya", GREEN, callback_data="fj_check")],
    ])


async def send_join_prompt(reply):
    await reply(join_text(), parse_mode=ParseMode.HTML, reply_markup=join_kb(),
                disable_web_page_preview=True)

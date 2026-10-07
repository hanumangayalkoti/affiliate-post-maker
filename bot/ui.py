"""
ui.py — chhote UI helpers:
  • btn()      rangeen inline button (Telegram ka 'style' field)
  • tr()       English / Hinglish text
  • chan()     channel ko @username (tap karke khule) ya naam
  • chat clean — naya command aane pe pichle 3 commands aur unke jawab delete.
                 Reports (payment, task bana, post report) kabhi track nahi hote,
                 isliye kabhi delete nahi hote.
"""
import asyncio
import html as html_lib
import logging

from telegram import InlineKeyboardButton

logger = logging.getLogger(__name__)
esc = html_lib.escape

GREEN = "success"
BLUE = "primary"
RED = "danger"

BUTTON_STYLES = {
    "":    ("⚪ Normal", "⚪ Normal"),
    GREEN: ("🟢 Green", "🟢 Hara"),
    BLUE:  ("🔵 Blue", "🔵 Neela"),
    RED:   ("🔴 Red", "🔴 Laal"),
}

TAGLINE = "Made by Affiliates, for Affiliates"


def btn(text: str, style: str = "", **kwargs) -> InlineKeyboardButton:
    """InlineKeyboardButton + rang. style khali = normal button."""
    if style in (GREEN, BLUE, RED):
        kwargs["api_kwargs"] = {**(kwargs.get("api_kwargs") or {}), "style": style}
    return InlineKeyboardButton(text, **kwargs)


def style_name(style: str, lang: str) -> str:
    pair = BUTTON_STYLES.get(style or "", BUTTON_STYLES[""])
    return pair[0] if lang == "en" else pair[1]


def next_style(style: str) -> str:
    keys = list(BUTTON_STYLES)
    try:
        return keys[(keys.index(style or "") + 1) % len(keys)]
    except ValueError:
        return ""


def tr(lang: str, en: str, hi: str) -> str:
    """lang 'en' → English, warna Hinglish."""
    return en if lang == "en" else hi


def chan(title: str = "", username: str = "", fallback: str = "—") -> str:
    """Channel dikhane ka tareeka — public ho to @username (tap karke khulta hai)."""
    u = (username or "").lstrip("@").strip()
    if u:
        return "@" + esc(u)
    t = (title or "").strip()
    return f"<b>{esc(t)}</b>" if t else fallback


# =============================================================================
# CHAT CLEAN
# =============================================================================
KEEP_GROUPS = 3          # pichle itne commands (aur unke jawab) delete honge
_TRAIL = "_trail"


def _trail(context) -> list:
    tr_ = context.user_data.get(_TRAIL)
    if not isinstance(tr_, list):
        tr_ = context.user_data[_TRAIL] = []
    return tr_


_locks: dict = {}


def user_lock(uid: int) -> asyncio.Lock:
    """Ek user ke commands ek-ek karke chalein. Do baar /start jaldi dabaya to dono
    saath chal ke ek doosre ki screen nahi bigaadte — doosra pehle wale ko saaf karta hai."""
    lock = _locks.get(uid)
    if lock is None:
        if len(_locks) > 20000:
            _locks.clear()
        lock = _locks[uid] = asyncio.Lock()
    return lock


def _uid(context):
    return getattr(context, "_user_id", None)


def _save(context):
    """Screen ke message IDs DB mein — bot restart ho tab bhi agla command purani screen saaf kare."""
    uid = _uid(context)
    if uid:
        from storage import screen_ids_set
        screen_ids_set(uid, [m for g in _trail(context) for m in g])


async def new_screen(context, bot, chat_id: int, user_msg_id: int = None):
    """
    Naya command aaya — pichli screen ke saare messages (bot ke jawab + user ke
    command) delete, aur naya group shuru. Sirf "screen" wale message jaate hain —
    reports (payment, post report, task bana...) kabhi track nahi hote, wo rehte hain.
    Telegram 48 ghante se purane message delete nahi karne deta.
    """
    trail = _trail(context)
    old = [m for g in trail[-KEEP_GROUPS:] for m in g]
    if not old and _uid(context):
        from storage import screen_ids_get
        old = screen_ids_get(_uid(context))     # restart ke baad memory khaali — DB se
    trail.clear()
    trail.append([user_msg_id] if user_msg_id else [])
    _save(context)
    for mid in dict.fromkeys(old):
        if mid == user_msg_id:
            continue
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


def track(context, msg):
    """Ye message 'screen' ka hissa hai — agle command pe delete hoga."""
    if msg is None or getattr(msg, "message_id", None) is None:
        return msg
    trail = _trail(context)
    if not trail:
        trail.append([])
    trail[-1].append(msg.message_id)
    if len(trail[-1]) > 40:
        trail[-1] = trail[-1][-40:]
    _save(context)
    return msg


def track_id(context, message_id: int):
    if message_id:
        trail = _trail(context)
        if not trail:
            trail.append([])
        trail[-1].append(message_id)
        _save(context)

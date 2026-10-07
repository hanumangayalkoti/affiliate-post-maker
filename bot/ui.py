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
# CHAT CLEAN — sirf LAGATAR SAME command ki duplicate screens hatao
# =============================================================================
# Rule:
#   • /start, jawab, /start, jawab, /start, jawab  → pehle wale /start + jawab
#     delete, sirf aakhri bache (har naye /start pe pichla wala hat-ta hai)
#   • Beech mein REPORT (ya koi bhi aur message) aaya → sequence toot gaya,
#     report se pehle wala kuch nahi chhua jaata. Reports kabhi delete nahi hote.
#   • Alag command (/start ke baad /help) → sequence toot gaya, kuch delete nahi.
#
# "Beech mein kuch aaya?" kaise pata: private chat mein Telegram har message ko
# line se number deta hai (user ke aur bot ke dono). Bot yaad rakhta hai ki screen
# ke kaun se number uske the ("known"). Pichli screen se naye command tak koi bhi
# number aisa mila jo known nahi — matlab beech mein report / deal / kuch aur aaya.
_SCREEN = "_screen"
KNOWN_MAX = 300


def _screen(context) -> dict:
    s = context.user_data.get(_SCREEN)
    if not isinstance(s, dict):
        s = context.user_data[_SCREEN] = {"cmd": None, "ids": [], "known": [], "loaded": False}
    return s


def command_name(text: str) -> str:
    """'/start ref_x' → 'start', '/Help@MyBot' → 'help'."""
    first = (text or "").strip().split(maxsplit=1)[0] if (text or "").strip() else ""
    if not first.startswith("/"):
        return ""
    return first[1:].split("@", 1)[0].lower()


_locks: dict = {}
_arrivals: dict = {}


def arrive(uid: int) -> int:
    """Command aaya — iska number. Lock milne pe dekhte hain koi naya to nahi aa gaya."""
    n = _arrivals.get(uid, 0) + 1
    _arrivals[uid] = n
    return n


def is_latest(uid: int, n: int) -> bool:
    return _arrivals.get(uid) == n


def user_lock(uid: int) -> asyncio.Lock:
    """Ek user ke commands ek-ek karke chalein (do /start ek saath screen nahi bigaadte)."""
    lock = _locks.get(uid)
    if lock is None:
        if len(_locks) > 20000:
            _locks.clear()
        lock = _locks[uid] = asyncio.Lock()
    return lock


def _uid(context):
    return getattr(context, "_user_id", None)


# Background kaam ke tasks ka reference rakhna zaroori hai — warna Python unhe
# beech mein hi mita sakta hai aur delete adhoora reh jaata hai.
_bg_tasks: set = set()


def _spawn(coro):
    t = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


_save_pending: set = set()


def _save(context):
    """Screen ki yaad DB mein (restart ke baad bhi) — background mein, thoda ruk ke, ek hi write."""
    uid = _uid(context)
    if not uid or uid in _save_pending:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _save_pending.add(uid)

    async def _later():
        try:
            await asyncio.sleep(0.5)
            s = _screen(context)
            from storage import screen_state_set
            snap = {"cmd": s.get("cmd"), "ids": list(s["ids"]), "known": list(s["known"])}
            await asyncio.get_running_loop().run_in_executor(None, screen_state_set, uid, snap)
        except Exception as e:
            logger.error(f"Screen save fail ({uid}): {e}")
        finally:
            _save_pending.discard(uid)

    _spawn(_later())


def _remember(s: dict, mid: int):
    if mid is None:
        return
    if mid not in s["ids"]:
        s["ids"].append(mid)
    if mid not in s["known"]:
        s["known"].append(mid)
        if len(s["known"]) > KNOWN_MAX:
            del s["known"][:-KNOWN_MAX]


def _nothing_between(s: dict, new_id: int) -> bool:
    """Pichli screen ke pehle message se naye command tak har message number bot ka
    jaana-pehchana hai? Ek bhi anjaan (report / deal / kuch aur) → False."""
    if not s["ids"] or not new_id:
        return False
    known = set(s["known"])
    return all(i in known for i in range(min(s["ids"]), new_id))


async def _raw_delete(bot, chat_id: int, ids: list):
    """
    Telegram ko seedha delete request — bot ke speed-controller (rate limiter) ko
    BYPASS karke. Wo controller "ruko" aane pe POORE bot ko (saare users ke liye)
    rok deta tha, sirf ek delete ki wajah se. Delete ka "ruko" sirf delete ko roke.
    """
    from telegram import Bot
    return await Bot._do_post(bot, "deleteMessages", {"chat_id": chat_id, "message_ids": ids})


async def _raw_delete_one(bot, chat_id: int, mid: int):
    from telegram import Bot
    return await Bot._do_post(bot, "deleteMessage", {"chat_id": chat_id, "message_id": mid})


async def _delete_many(bot, chat_id: int, ids: list):
    """
    Messages EK request mein delete (100 tak). Error aaye to bot crash nahi hota —
    log mein likhte hain aur baaki messages delete karte rehte hain.
    """
    from telegram.error import RetryAfter
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        for attempt in range(3):
            try:
                await _raw_delete(bot, chat_id, chunk)
                logger.info(f"Chat clean: {len(chunk)} message delete ({chat_id})")
                break
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 3)) + 0.5
                logger.warning(f"Chat clean: Telegram ne {wait:.0f}s rukne ko kaha ({chat_id}), phir try")
                await asyncio.sleep(wait)                # sirf ye background kaam rukta hai, bot nahi
            except Exception as e:
                logger.warning(f"Chat clean: bulk delete fail ({chat_id}) {chunk}: {e} — ek-ek karke")
                for mid in chunk:
                    try:
                        await _raw_delete_one(bot, chat_id, mid)
                    except Exception as e1:
                        # bahut purana / pehle hi delete / permission nahi — log karo, aage badho
                        logger.warning(f"Chat clean: message {mid} delete nahi hua ({chat_id}): {e1}")
                break
        else:
            logger.warning(f"Chat clean: {chunk} delete nahi ho paaye ({chat_id}) — Telegram limit")


async def new_screen(context, bot, chat_id: int, user_msg_id: int = None, cmd: str = None):
    """
    Naya command aaya. Agar ye WAHI command hai jo pichli screen ka tha, aur beech
    mein koi aur message (report wagaira) nahi aaya — to pichli screen (command +
    bot ke jawab) delete. Warna kuch delete nahi, bas nayi screen shuru.
    Delete background mein — naya jawab turant jaata hai.
    """
    s = _screen(context)
    if not s.get("loaded") and not s["ids"] and _uid(context):
        from storage import screen_state_get    # restart ke baad memory khaali — DB se
        saved = await asyncio.get_running_loop().run_in_executor(None, screen_state_get, _uid(context))
        s.update(cmd=saved["cmd"], ids=saved["ids"], known=saved["known"])
    s["loaded"] = True

    old = []
    if cmd and s.get("cmd") == cmd and _nothing_between(s, user_msg_id):
        old = [m for m in dict.fromkeys(s["ids"]) if m != user_msg_id]

    s["cmd"] = cmd
    s["ids"] = []
    _remember(s, user_msg_id)
    _save(context)
    if old:
        _spawn(_delete_many(bot, chat_id, old))


def track(context, msg):
    """Ye message abhi ki screen ka hissa hai (report NAHI) — agla same command ise hata sakta hai."""
    if msg is None or getattr(msg, "message_id", None) is None:
        return msg
    s = _screen(context)
    _remember(s, msg.message_id)
    if len(s["ids"]) > 80:
        s["ids"] = s["ids"][-80:]
    _save(context)
    return msg


def track_id(context, message_id: int, cmd: str = None):
    """
    Jaldi-jaldi aaya command jiska jawab skip hua (spam). Same command ho to abhi
    ki screen mein jodo (agle wale ke saath hatega); alag command ho to sequence
    toot gaya — uski nayi screen shuru (kuch delete nahi).
    """
    if not message_id:
        return
    s = _screen(context)
    if cmd is not None and s.get("cmd") != cmd:
        s["cmd"] = cmd
        s["ids"] = []
    _remember(s, message_id)
    _save(context)


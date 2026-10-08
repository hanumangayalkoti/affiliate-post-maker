"""
alerts.py — admin ko khabar (naya user, trial, payment, task bana/hata,
plan khatam...). Ye messages kabhi delete nahi hote.
"""
import html as html_lib
import logging
from datetime import datetime, timezone

from telegram.constants import ParseMode

from storage import to_local, patch_task_cfg
from users import ADMIN_IDS, get_user, is_admin, user_counts

logger = logging.getLogger(__name__)
esc = html_lib.escape


def who(uid: int, u: dict = None) -> str:
    u = u or get_user(uid) or {}
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f"{name}{un} <code>{uid}</code>".strip()


def _ist(dt) -> str:
    if not dt:
        return "—"
    try:
        return to_local(dt).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return "—"


def user_card(uid: int, tg_user=None) -> list:
    """Admin ke liye user ki poori pehchaan — naam, username, ID, join time, referral."""
    u = get_user(uid) or {}
    name = esc(u.get("first_name") or getattr(tg_user, "first_name", "") or "—")
    uname = u.get("username") or getattr(tg_user, "username", "") or ""
    lines = [f"👤 Naam: <b>{name}</b>",
             f"🔗 Username: {'@' + esc(uname) if uname else '— (nahi hai)'}",
             f"🆔 User ID: <code>{uid}</code>"]
    tg_lang = getattr(tg_user, "language_code", "") or ""
    if tg_lang:
        lines.append(f"🌐 Telegram language: {esc(tg_lang)}")
    lines.append(f"📅 Join: {_ist(u.get('joined_at'))}")
    try:
        from referral import referrer_of
        ref = referrer_of(uid)
    except Exception:
        ref = None
    lines.append(f"🎁 Referred by: {who(ref) if ref else '— (seedha aaya)'}")
    return lines


def users_line() -> str:
    c = user_counts()
    return f"👥 Kul users: <b>{c.get('total', 0)}</b> • Aaj naye: <b>{c.get('new_today', 0)}</b>"


def now_ist() -> str:
    return _ist(datetime.now(timezone.utc))


async def notify_admins(bot, text: str, about_uid: int = None):
    """Admin ko HTML message. Admin khud kuch kare to khud ko nahi bhejte."""
    if about_uid and is_admin(about_uid):
        return
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Admin notify fail ({aid}): {e}")


async def _channel_link(bot, uid: int, task: dict, kind: str) -> str:
    """Draft / Destination ka link admin ke liye. Public = t.me/username.
    Private = bot ka banaya invite link (ek baar bana ke task mein yaad rakhte hain)."""
    c = task["cfg"]
    if kind == "dest":
        cid, title, uname, key = c.get("channel"), c.get("channel_title"), c.get("channel_username"), "dest_invite"
    else:
        cid, title, uname, key = (c.get("source_channel"), c.get("source_title"),
                                  c.get("source_username"), "source_invite")
    if not cid:
        return "— (set nahi)"
    name = esc(title or str(cid))
    uname = (uname or "").lstrip("@").strip()
    if uname:
        return f'<a href="https://t.me/{esc(uname)}">{name}</a> (@{esc(uname)})'
    link = c.get(key) if c.get(key + "_for") == str(cid) else ""
    if not link:
        try:
            inv = await bot.create_chat_invite_link(chat_id=int(cid), name="Admin view")
            link = inv.invite_link
            c[key], c[key + "_for"] = link, str(cid)
            # Sirf invite link likho — `task` purani copy ho sakti hai (invite
            # banne mein der lagti hai), poora cfg save kiya to user ki nayi
            # settings (Discount Filter, buttons...) mit jaati thi.
            patch_task_cfg(uid, task["id"], {key: link, key + "_for": str(cid)})
        except Exception as e:
            logger.info(f"Invite link nahi bana ({cid}): {e}")
            link = ""
    if link:
        return f'<a href="{esc(link)}">{name}</a> (private)'
    return f"{name} (private, <code>{esc(str(cid))}</code>)"


async def notify_task_event(bot, uid: int, task: dict, event: str):
    """Admin ko task ki poori khabar — kab, kisne, kaunsa task, tag, Draft aur Destination link."""
    if is_admin(uid) or not task:
        return
    c = task["cfg"]
    when = to_local(datetime.now(timezone.utc)).strftime("%d %b %Y, %I:%M %p IST")
    text = (f"{event}\n"
            f"🕒 {when}\n"
            f"👤 {who(uid)}\n"
            f"📋 Task: <b>{esc(c.get('name') or '#' + str(task['id']))}</b> (#{task['id']})"
            f"{'  ⏸️ paused' if task.get('paused') else ''}\n"
            f"🏷️ Tag: <code>{esc(c.get('tag') or '—')}</code>\n"
            f"📥 Draft: {await _channel_link(bot, uid, task, 'src')}\n"
            f"📢 Destination: {await _channel_link(bot, uid, task, 'dest')}")
    await notify_admins(bot, text, uid)

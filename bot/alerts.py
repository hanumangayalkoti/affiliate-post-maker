"""
alerts.py — admin ko khabar (naya user, trial, payment, task bana/hata,
plan khatam...). Ye messages kabhi delete nahi hote.
"""
import html as html_lib
import logging

from telegram.constants import ParseMode

from users import ADMIN_IDS, get_user, is_admin

logger = logging.getLogger(__name__)
esc = html_lib.escape


def who(uid: int, u: dict = None) -> str:
    u = u or get_user(uid) or {}
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f"{name}{un} <code>{uid}</code>".strip()


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

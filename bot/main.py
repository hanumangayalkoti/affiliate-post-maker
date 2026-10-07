"""
Deal Post Maker Bot — Made by Affiliates, for Affiliates.

User apna Amazon affiliate tag aur channel set karke Amazon (aur baaki) deals
ko sundar posts mein badal sakta hai. Plans: Basic / Pro / Premium, naye user
ko 7 din Pro free. Har post ek TASK (Draft ➜ Destination) ke hisaab se jaati hai.
"""
import os

# Railway template mein jo value nahi bhari ("PASTE_HERE") use khaali maano —
# baaki saari files import hone se PEHLE, kyunki wo env yahin se padhti hain.
for _k, _v in list(os.environ.items()):
    if _v.strip().upper() in ("PASTE_HERE", "PASTE HERE", "CHANGE_ME"):
        del os.environ[_k]

import time
import asyncio
import logging
import datetime as dt
import html as html_lib

from telegram import Update, InlineKeyboardMarkup, BotCommand, BotCommandScopeChat
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler,
    ChatMemberHandler, PreCheckoutQueryHandler, filters, ContextTypes, AIORateLimiter,
)

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(name)s — %(message)s",
                    level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger("main")

import admin
import billing
import faq
import gate
import task_ui
import referral
from alerts import notify_admins, who
from database import (
    cache_cleanup, post_log_cleanup, cleanup_old_entries, user_stats, posts_today, day_report_rows,
)
from engine import (
    process_and_post, is_own_message, extract_urls, hidden_link_urls, get_amazon_urls_deep,
    remember_own, dm_user, chat_matches, fmt_date, day_start_naive, SELF_MARKER,
)
from storage import (
    init_db, list_tasks, create_task, new_task_config, find_tasks_by_source, find_tasks_by_dest,
    LOCAL_TZ,
)
from tiers import TRIAL_DAYS, TIERS
from ui import btn, tr, chan, new_screen, track, track_id, user_lock, arrive, is_latest, GREEN, BLUE, TAGLINE
from users import (
    ADMIN_IDS, OWNER_ID, is_admin, is_active, is_blocked, get_user, upsert_user, get_lang,
    set_lang, set_default_task, days_left, limits, start_trial, users_expiring_soon,
    users_just_expired, set_remind_stage,
)

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_NAME = os.getenv("BOT_NAME", "Deal Post Maker")
CACHE_KEEP_DAYS = int(os.getenv("CACHE_KEEP_DAYS", "5"))
esc = html_lib.escape

_inactive_notice: dict = {}     # draft channel owner ko baar-baar "plan khatam" na bole


# =============================================================================
# HOME
# =============================================================================
def home_text(uid: int, first_name: str) -> str:
    lang = get_lang(uid)
    u = get_user(uid) or {}
    lim = limits(uid, u)
    tasks = list_tasks(uid)
    run = task_ui.running_ids(uid)
    d = task_ui.default_task(uid)

    if is_admin(uid):
        plan = "👑 Admin"
    elif is_active(uid, u):
        trial = " (free trial)" if u.get("is_trial") else ""
        plan = tr(lang, f"{lim['emoji']} {lim['name']}{trial} — {days_left(u):.0f} days left",
                  f"{lim['emoji']} {lim['name']}{trial} — {days_left(u):.0f} din baaki")
    else:
        plan = tr(lang, "❌ No active plan", "❌ Koi plan chalu nahi")

    today = posts_today(uid)
    if is_admin(uid):
        used = f"{today} (∞)"
    else:
        used = tr(lang, f"{today} (limit {lim['daily']} per task)", f"{today} (limit {lim['daily']} har task)")

    lines = [
        tr(lang, f"👋 <b>Hello {esc(first_name or 'friend')}!</b>", f"👋 <b>Namaste {esc(first_name or 'dost')}!</b>"),
        f"🤖 <b>{esc(BOT_NAME)}</b>\n<i>{TAGLINE}</i>\n",
        f"💎 Plan: <b>{plan}</b>",
        tr(lang, f"📤 Today: <b>{used}</b> posts", f"📤 Aaj: <b>{used}</b> post"),
        tr(lang, f"📋 Tasks: <b>{len(run)}</b> running / {len(tasks)}",
           f"📋 Tasks: <b>{len(run)}</b> chal rahe / {len(tasks)}"),
    ]
    if d:
        c = d["cfg"]
        tag_ok = bool((c.get("tag") or "").strip())
        dst_ok = bool(str(c.get("channel") or "").strip())
        if tag_ok and dst_ok and is_active(uid, u):
            where = chan(c.get("channel_title"), c.get("channel_username"))
            lines.append(tr(lang,
                            f"\n🚀 <b>All set!</b> Send me any Amazon link — I'll post it to {where} with your tag.",
                            f"\n🚀 <b>Sab ready hai!</b> Mujhe koi bhi Amazon link bhejein — main use {where} "
                            f"pe aapke tag ke saath post kar dunga."))
        else:
            lines.append(tr(lang, "\n<b>Finish setup:</b>", "\n<b>Setup poora karein:</b>"))
            lines.append(f"{'✅' if tag_ok else '❌'} 🏷️ Affiliate Tag")
            lines.append(f"{'✅' if dst_ok else '❌'} 📢 Destination channel")
            if not is_active(uid, u):
                lines.append("❌ 💎 Plan")
    elif not tasks:
        lines.append(tr(lang, "\nCreate your first task to start posting 👇",
                        "\nPosting shuru karne ke liye pehla task banayein 👇"))
    return "\n".join(lines)


def home_kb(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    rows = []
    d = task_ui.default_task(uid)
    if d:
        c = d["cfg"]
        if not (c.get("tag") or "").strip() or not str(c.get("channel") or "").strip():
            rows.append([btn(tr(lang, "🚀 Finish Setup", "🚀 Setup poora karein"), callback_data=f"t:{d['id']}")])
    elif not list_tasks(uid):
        rows.append([btn(tr(lang, "➕ Create First Task", "➕ Pehla Task banayein"), GREEN, callback_data="tn")])
    if not is_active(uid):
        rows.append([btn(tr(lang, "💎 See Plans", "💎 Plans dekhein"), BLUE, callback_data="open_plan")])
    rows += [
        [btn(tr(lang, "📋 My Tasks", "📋 Mere Tasks"), callback_data="tl"),
         btn("💎 Plan", callback_data="open_plan")],
        [btn("⚙️ Config", callback_data="cfg:"),
         btn("📊 Stats", callback_data="menu_stats")],
        [btn(tr(lang, "🎁 Refer & Earn", "🎁 Refer & Earn"), GREEN, callback_data="ref:home"),
         btn("❓ Help & FAQ", callback_data="menu_help")],
        [btn("🌐 Language", callback_data="menu_lang")],
    ]
    if is_admin(uid):
        rows.append([btn("👑 Admin Panel", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def help_text(uid: int) -> str:
    lang = get_lang(uid)
    t = tr(lang,
           f"📖 <b>How to use {esc(BOT_NAME)}</b>\n\n"
           "<b>Posting:</b>\n• Send me an Amazon link (several links in one message = separate posts)\n"
           "• Or put deals in your Draft channel — I'll pick them up\n• Non-Amazon posts work too\n\n"
           "<b>Commands:</b>\n🏠 /start — Home\n📋 /tasks — Your tasks & all settings\n"
           "⚙️ /config — See all settings of a task at a glance\n"
           "📊 /stats — Your posts\n💎 /plan — Plans & payment\n🎁 /refer — Refer & Earn\n"
           "❓ /faq — Common questions\n🌐 /language — Change language\n🧾 /paysupport — Payment help\n",
           f"📖 <b>{esc(BOT_NAME)} — Kaise use karein</b>\n\n"
           "<b>Post kaise karein:</b>\n• Mujhe Amazon link bhejein (ek message mein kai link = alag-alag post)\n"
           "• Ya apne Draft channel mein deal daalein — main khud utha lunga\n• Non-Amazon posts bhi chalti hain\n\n"
           "<b>Commands:</b>\n🏠 /start — Home\n📋 /tasks — Aapke tasks aur saari settings\n"
           "⚙️ /config — Task ki saari settings ek nazar mein\n"
           "📊 /stats — Aapki posts\n💎 /plan — Plans aur payment\n🎁 /refer — Refer karke kamaayein\n"
           "❓ /faq — Aam sawaal\n🌐 /language — Bhasha badlein\n🧾 /paysupport — Payment mein madad\n")
    s = billing.support_line(lang)
    if s:
        t += "\n" + s
    if is_admin(uid):
        t += "\n\n👑 <b>Admin:</b> /admin — Panel  •  /user ID  •  /broadcast  •  /withdrawals"
    return t


def help_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("❓ FAQ", BLUE, callback_data="faq"),
                                  btn("🏠 Home", callback_data="home")]])


def stats_text(uid: int) -> str:
    lang = get_lang(uid)
    st = user_stats(uid, day_start_naive())
    lim = limits(uid)
    daily = lim["daily"] if lim.get("key") != "admin" else "∞"
    lines = [tr(lang, "📊 <b>Your Posts</b>\n", "📊 <b>Aapki Posts</b>\n"),
             tr(lang, f"📅 Today: <b>{st['today']}</b>  (limit {daily} per task)",
                f"📅 Aaj: <b>{st['today']}</b>  (limit {daily} har task)"),
             tr(lang, f"🗓️ 7 days: <b>{st['week']}</b>", f"🗓️ 7 din: <b>{st['week']}</b>"),
             tr(lang, f"📆 30 days: <b>{st['month']}</b>  (Amazon {st['amazon_month']}, other {st['other_month']})",
                f"📆 30 din: <b>{st['month']}</b>  (Amazon {st['amazon_month']}, baaki {st['other_month']})"),
             f"🏆 Total: <b>{st['total']}</b>"]
    tasks = list_tasks(uid)
    if tasks:
        lines.append(tr(lang, "\n<b>Today by task:</b>", "\n<b>Aaj — task ke hisaab se:</b>"))
        for t in tasks:
            lines.append(f"• {esc(task_ui.tname(t, lang))}: {st['by_task'].get(t['id'], 0)} / {daily}")
    lines.append(tr(lang, "\n<i>The daily count resets at 12:00 midnight. Clicks & earnings are in your "
                          "Amazon Associates dashboard.</i>",
                    "\n<i>Roz ki ginti raat 12 baje reset hoti hai. Clicks aur kamai Amazon Associates "
                    "dashboard mein dikhegi.</i>"))
    return "\n".join(lines)


LANG_PICK_TEXT = "🌐 <b>Choose your language</b>\n🌐 <b>Apni bhasha chunein</b>"


def lang_pick_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🇬🇧 English", callback_data="lang:en"),
                                  btn("🇮🇳 Hinglish", callback_data="lang:hi")],
                                 [btn("🏠 Home", callback_data="home")]])


# =============================================================================
# GATE: language → join → trial + pehla task
# =============================================================================
async def gate_screen(bot, uid: int):
    """Gate pass nahi hua to (text, kb) jo dikhana hai; pass hai to None."""
    if not gate.has_lang(uid) and not is_admin(uid):
        return gate.lang_text(), gate.lang_kb()
    if not await gate.is_joined(bot, uid):
        lang = get_lang(uid)
        return gate.join_text(lang), gate.join_kb(lang)
    return None


async def after_gate(bot, uid: int):
    """Gate ke baad: 7 din trial (ek hi baar) + pehla task."""
    u = get_user(uid) or {}
    lang = get_lang(uid)
    if not is_admin(uid) and not u.get("trial_used") and not u.get("expires_at"):
        exp = start_trial(uid)
        if exp:
            pro = TIERS["pro"]
            await dm_user(bot, uid,
                          tr(lang, f"🎁 <b>Your {TRIAL_DAYS}-day Pro trial has started — free!</b>\n"
                                   f"📅 Valid till {fmt_date(exp)}\n📋 {pro['tasks']} tasks • "
                                   f"📤 {pro['daily']} posts/day per task • 🎨 Image Card",
                             f"🎁 <b>Aapka {TRIAL_DAYS} din ka Pro trial shuru — bilkul free!</b>\n"
                             f"📅 {fmt_date(exp)} tak\n📋 {pro['tasks']} task • "
                             f"📤 {pro['daily']} post/din har task • 🎨 Image Card"),
                          parse_mode=ParseMode.HTML)
            await notify_admins(bot, f"🎁 <b>Trial shuru</b>: {who(uid)}", uid)
            # Pehli baar hi Task 1 banta hai — user ne baad mein delete kiya to wapas nahi
            if not list_tasks(uid):
                tid = create_task(uid, new_task_config("Task 1"))
                if tid:
                    set_default_task(uid, tid)


# =============================================================================
# WRAPPERS
# =============================================================================
def _touch(update: Update):
    """(uid, naya_user?)"""
    u = update.effective_user
    if not u:
        return 0, False
    return u.id, upsert_user(u.id, u.username or "", u.first_name or "")


# Bot jis jawab ka intezaar kar raha tha — naya command aane pe user ko naam se batate hain
ACTION_NAMES = {
    "t_tag":       ("setting the Affiliate Tag", "Affiliate Tag set karna"),
    "t_dest":      ("setting the Destination channel", "Destination channel set karna"),
    "t_src":       ("setting the Draft channel", "Draft channel set karna"),
    "t_name":      ("renaming the task", "Task ka naam badalna"),
    "t_hf":        ("setting the Header / Footer text", "Header / Footer ka text set karna"),
    "t_wm":        ("setting the Watermark text", "Watermark ka text set karna"),
    "t_btn_label": ("setting the button name", "Button ka naam set karna"),
    "t_btn_link":  ("setting the button link", "Button ka link set karna"),
    "adm_find":    ("finding a user", "User dhoondhna"),
    "adm_bc":      ("broadcast", "Broadcast"),
    "adm_days":    ("adding / reducing days", "Din jodna / kaatna"),
    "adm_msg":     ("messaging a user", "User ko message bhejna"),
    "ref_pm":      ("setting the payout method", "Payout method set karna"),
}
_ACTION_KEYS = ("action", "tid", "bkey", "hf_kind", "adm_target", "adm_sign", "adm_back", "bc_src",
                "ref_method")


def _drop_pending(context) -> str:
    """Adhoora kaam (bot jawab ka intezaar kar raha tha) band karo. Uska key wapas."""
    prev = context.user_data.get("action") or ""
    for k in _ACTION_KEYS:
        context.user_data.pop(k, None)
    return prev


def _pending_note(prev: str, lang: str) -> str:
    en, hi = ACTION_NAMES.get(prev, ("the previous step", "Pichla kaam"))
    return tr(lang, f"⚠️ <b>Previous action cancelled:</b> {en}.\nYou can start it again any time.",
              f"⚠️ <b>Pichla kaam band kar diya:</b> {hi}.\nJab chahein dobara shuru kar sakte hain.")


def user_command(fn):
    """Har user command: register, block, chat clean, gate. Ek user ke commands
    ek-ek karke chalte hain (lock), taaki chat clean gadbad na ho."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        if not uid:
            return
        n = arrive(uid)
        async with user_lock(uid):
            if not is_latest(uid, n) and update.message:
                # User ne jaldi-jaldi kai command bheje (jaise 30 baar /start) — sirf
                # AAKHRI wala jawab dega. Ye wala skip, iska message agli screen ke
                # saath delete hoga. Warna har ek ka jawab + Telegram speed-limit = minute bhar ki der.
                track_id(context, update.message.message_id)
                if fn is cmd_start and getattr(context, "args", None):
                    try:                               # skip hua /start ref_ — referral phir bhi jodo
                        await referral.handle_start_arg(context.bot, uid, context.args[0])
                    except Exception as e:
                        logger.error(f"Referral start fail ({uid}): {e}")
                return
            await _user_command(fn, update, context)
    return wrapper


async def _user_command(fn, update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, is_new = _touch(update)
    if not uid:
        return
    msg = update.message
    if is_new:
        await notify_admins(context.bot, f"🆕 <b>Naya user</b>: {who(uid)}", uid)
    if is_blocked(uid):
        lang = get_lang(uid)
        await msg.reply_text("⛔ " + tr(lang, "Your access has been blocked.",
                                        "Aapka access band kar diya gaya hai.") + "\n" + billing.support_line(lang))
        return
    if fn is cmd_start and getattr(context, "args", None):
        # Referral link (/start ref_CODE) gate se PEHLE — naya user pehle language /
        # join screen dekhta hai aur cmd_start tak pahunchta hi nahi tha, to code kho
        # jaata tha aur naye user ka referral judta hi nahi tha.
        try:
            await referral.handle_start_arg(context.bot, uid, context.args[0])
        except Exception as e:
            logger.error(f"Referral start fail ({uid}): {e}")
    await new_screen(context, context.bot, msg.chat_id, msg.message_id)
    prev = _drop_pending(context)
    if prev and fn is cmd_cancel:
        context.user_data["_cancelled"] = prev
    elif prev:
        track(context, await msg.reply_text(_pending_note(prev, get_lang(uid)), parse_mode=ParseMode.HTML))
    g = await gate_screen(context.bot, uid)
    if g:
        track(context, await msg.reply_text(g[0], parse_mode=ParseMode.HTML, reply_markup=g[1],
                                            disable_web_page_preview=True))
        return
    await after_gate(context.bot, uid)
    await fn(update, context, uid)


def admin_command(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not is_admin(u.id):
            return
        n = arrive(u.id)
        async with user_lock(u.id):
            if not is_latest(u.id, n):
                track_id(context, update.message.message_id)
                return
            await new_screen(context, context.bot, update.message.chat_id, update.message.message_id)
            prev = _drop_pending(context)
            if prev:
                track(context, await update.message.reply_text(_pending_note(prev, get_lang(u.id)),
                                                               parse_mode=ParseMode.HTML))
            await fn(update, context, u.id)
    return wrapper


async def _need_plan(context, reply, uid: int):
    lang = get_lang(uid)
    u = get_user(uid) or {}
    msg = (tr(lang, "⌛ <b>Your plan has ended.</b>", "⌛ <b>Aapka plan khatam ho gaya hai.</b>")
           if u.get("expires_at") else
           tr(lang, "💎 <b>You need a plan to post.</b>", "💎 <b>Post karne ke liye plan chahiye.</b>"))
    basic = TIERS["basic"]["inr"]
    m = await reply(msg + tr(lang, f"\n\nPlans start at just ₹{basic}/month.",
                             f"\n\nPlans sirf ₹{basic}/mahine se shuru."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[btn(tr(lang, "💎 See Plans", "💎 Plans dekhein"), BLUE,
                                                            callback_data="open_plan")]]))
    track(context, m)


# =============================================================================
# COMMANDS
# =============================================================================
async def _send(update, context, text, kb=None):
    m = await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                        disable_web_page_preview=True)
    track(context, m)


HOME_ROW = [btn("🏠 Home", callback_data="home")]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    await _send(update, context, home_text(uid, update.effective_user.first_name), home_kb(uid))


async def cmd_tasks(update, context, uid):
    lang = get_lang(uid)
    await _send(update, context, task_ui.tasks_text(uid, lang), task_ui.tasks_kb(uid, lang))


def _config_screen(uid: int, tid: int = None):
    lang = get_lang(uid)
    tasks = list_tasks(uid)
    if not tasks:
        return (tr(lang, "⚙️ You have no task yet. Create one in /tasks.",
                   "⚙️ Abhi koi task nahi hai. /tasks mein banayein."),
                InlineKeyboardMarkup([[btn(tr(lang, "📋 My Tasks", "📋 Mere Tasks"), callback_data="tl")]]))
    task = next((t for t in tasks if t["id"] == tid), None) or task_ui.default_task(uid) or tasks[0]
    return task_ui.config_text(uid, task, lang), task_ui.config_kb(uid, task, lang)


async def cmd_config(update, context, uid):
    text, kb = _config_screen(uid)
    await _send(update, context, text, kb)


async def cmd_refer(update, context, uid):
    text, kb = await referral.refer_screen(context.bot, uid, get_lang(uid))
    await _send(update, context, text, kb)


async def cmd_help(update, context, uid):
    await _send(update, context, help_text(uid), help_kb(uid))


async def cmd_faq(update, context, uid):
    lang = get_lang(uid)
    await _send(update, context, faq.faq_text(lang), faq.faq_kb(lang))


async def cmd_stats(update, context, uid):
    await _send(update, context, stats_text(uid), InlineKeyboardMarkup([HOME_ROW]))


async def cmd_language(update, context, uid):
    await _send(update, context, LANG_PICK_TEXT, lang_pick_kb())


async def cmd_cancel(update, context, uid):
    lang = get_lang(uid)
    prev = context.user_data.pop("_cancelled", "") or _drop_pending(context)
    if prev:
        en, hi = ACTION_NAMES.get(prev, ("the previous step", "Pichla kaam"))
        text = tr(lang, f"❌ <b>Cancelled:</b> {en}.", f"❌ <b>Band kar diya:</b> {hi}.")
    else:
        text = tr(lang, "ℹ️ Nothing was pending to cancel.", "ℹ️ Band karne ko koi kaam chal nahi raha tha.")
    await _send(update, context, text, InlineKeyboardMarkup([HOME_ROW]))


async def _has_amazon_link(msg) -> bool:
    text = (msg.text or msg.caption or "")
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    return bool(await get_amazon_urls_deep(extract_urls(text) + hidden_link_urls(ents)))


# =============================================================================
# DM MESSAGES — deal ya kisi setting ka jawab
# =============================================================================
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    uid, is_new = _touch(update)
    if is_new:
        await notify_admins(context.bot, f"🆕 <b>Naya user</b>: {who(uid)}", uid)
    lang = get_lang(uid)
    if is_blocked(uid):
        await msg.reply_text("⛔ " + tr(lang, "Your access has been blocked.", "Aapka access band kar diya gaya hai."))
        return
    g = await gate_screen(context.bot, uid)
    if g:
        await new_screen(context, context.bot, msg.chat_id, msg.message_id)
        track(context, await msg.reply_text(g[0], parse_mode=ParseMode.HTML, reply_markup=g[1],
                                            disable_web_page_preview=True))
        return
    await after_gate(context.bot, uid)

    action = (context.user_data or {}).get("action")
    # Bot ne kuch aur poocha tha (tag / naam / text) par user ne deal bhej di —
    # sawaal chhodo, deal post karo. Jin sawaalon ka jawab link hi hai unhe nahi chhedte.
    if action and action not in task_ui.LINK_ACTIONS and await _has_amazon_link(msg):
        context.user_data.pop("action", None)
        action = None
    if action:
        if await referral.handle_input(update, context, uid, action):
            return
        if await admin.handle_admin_input(update, context, uid, action):
            return
        if await task_ui.handle_task_input(update, context, uid, action):
            return
        context.user_data.pop("action", None)

    if not is_active(uid):
        await _need_plan(context, msg.reply_text, uid)
        return

    task = task_ui.default_task(uid)
    if not task:
        m = await msg.reply_text(
            tr(lang, "📋 You have no running task. Create or resume one in /tasks.",
               "📋 Koi task chal nahi raha. /tasks mein task banayein ya chalu karein."),
            reply_markup=InlineKeyboardMarkup([[btn(tr(lang, "📋 My Tasks", "📋 Mere Tasks"), callback_data="tl")]]))
        track(context, m)
        return

    async def notify(text, **kwargs):
        try:
            return await msg.reply_text(text, **kwargs)       # post report — delete nahi hota
        except Exception as e:
            logger.error(f"DM reply fail: {e}")
            return None

    await process_and_post(context, uid, msg, notify, task, lang)


# =============================================================================
# DRAFT CHANNEL POSTS
# =============================================================================
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or is_own_message(msg, context.bot.id):
        return
    tasks = find_tasks_by_source(msg.chat.id, msg.chat.username or "")
    if not tasks:
        return
    owner = tasks[0]["user_id"]              # ek Draft = ek owner
    tasks = [t for t in tasks
             if t["user_id"] == owner and chat_matches(msg.chat, t["cfg"].get("source_channel"))]
    if not tasks or is_blocked(owner):
        return
    lang = get_lang(owner)
    if not is_active(owner):
        if time.time() - _inactive_notice.get(owner, 0) > 6 * 3600:
            _inactive_notice[owner] = time.time()
            await dm_user(context.bot, owner,
                          tr(lang, "⌛ A deal came in your Draft channel, but your plan has ended — not posted. "
                                   "Renew from /plan.",
                             "⌛ Draft channel mein deal aayi, par aapka plan khatam hai — post nahi ki. "
                             "/plan se renew karein."))
        return

    run = task_ui.running_ids(owner)
    for task in tasks:
        if task["id"] not in run:
            continue
        src_name = msg.chat.title or task["cfg"].get("source_title") or "Draft"
        source_tag = f"\n📥 Draft: <b>{esc(src_name)}</b>"

        async def notify(text, _uid=owner, **kwargs):
            try:
                sent = await msg.reply_text(text + SELF_MARKER, disable_notification=True, **kwargs)
                return remember_own(sent)
            except Exception as e:
                logger.error(f"Draft reply fail: {e} — DM pe bhej raha hoon")
                return await dm_user(context.bot, _uid, text, **kwargs)

        await process_and_post(context, owner, msg, notify, task, lang, source_tag=source_tag)


# =============================================================================
# BOT KO CHANNEL MEIN ADMIN BANAYA / HATAYA
# =============================================================================
async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    if not cm or cm.chat.type != "channel":
        return
    chat, new = cm.chat, cm.new_chat_member
    adder = cm.from_user.id if cm.from_user else 0

    if new.status == "administrator":
        if not adder or not get_user(adder) or is_blocked(adder):
            return
        for t in list_tasks(adder):
            c = t["cfg"]
            if chat_matches(chat, c.get("channel")) or chat_matches(chat, c.get("source_channel")):
                return
        await task_ui.channel_added_prompt(context.bot, adder, chat,
                                           bool(getattr(new, "can_post_messages", False)))
        return

    if new.status in ("left", "kicked", "member", "restricted"):
        told = set()
        for t in find_tasks_by_dest(chat.id, chat.username or ""):
            uid = t["user_id"]
            if uid in told:
                continue
            told.add(uid)
            lang = get_lang(uid)
            where = chan(chat.title, chat.username)
            await dm_user(context.bot, uid,
                          tr(lang, f"⚠️ I was removed from {where} (or I'm no longer an admin). Posts can't go "
                                   "there now. Make me an admin again, or set a new Destination in /tasks.",
                             f"⚠️ Mujhe {where} se hata diya gaya (ya admin nahi raha). Ab wahan post nahi "
                             "ho payegi. Dobara admin banayein ya /tasks mein naya Destination set karein."),
                          parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    try:
        await _route_callback(query, context, query.from_user.id, query.data or "")
    finally:
        try:
            await query.answer()          # handler ne pehle hi alert diya ho to ye chup-chaap fail
        except Exception:
            pass


async def _route_callback(query, context, uid: int, data: str):
    if not get_user(uid):
        if upsert_user(uid, query.from_user.username or "", query.from_user.first_name or ""):
            await notify_admins(context.bot, f"🆕 <b>Naya user</b>: {who(uid)}", uid)
    if is_blocked(uid):
        await query.answer("⛔", show_alert=True)
        return
    show = task_ui.show
    first = query.from_user.first_name

    # ── Language ─────────────────────────────────────────────────────────
    if data.startswith("lang:"):
        set_lang(uid, data.split(":", 1)[1])
        g = await gate_screen(context.bot, uid)
        if g:
            await show(query, context, g[0], g[1])
            return
        await after_gate(context.bot, uid)
        await show(query, context, home_text(uid, first), home_kb(uid))
        return

    # ── Join check ───────────────────────────────────────────────────────
    if data == "fj_check":
        lang = get_lang(uid)
        if await gate.is_joined(context.bot, uid, fresh=True):
            await query.answer(tr(lang, "✅ Thank you! Let's start 🚀", "✅ Shukriya! Ab shuru karte hain 🚀"))
            await after_gate(context.bot, uid)
            await show(query, context, home_text(uid, first), home_kb(uid))
        else:
            await query.answer(gate.not_joined_alert(lang), show_alert=True)
        return

    g = await gate_screen(context.bot, uid)
    if g:
        await show(query, context, g[0], g[1])
        return
    lang = get_lang(uid)

    if data in ("home", "cancel"):
        for k in ("action", "tid", "bkey", "hf_kind"):
            context.user_data.pop(k, None)
        await show(query, context, home_text(uid, first), home_kb(uid))
        return
    if data.startswith("cfg:"):
        tid = data.split(":", 1)[1]
        text, kb = _config_screen(uid, int(tid) if tid.isdigit() else None)
        await show(query, context, text, kb)
        return
    if data == "menu_help":
        await show(query, context, help_text(uid), help_kb(uid))
        return
    if data == "menu_stats":
        await show(query, context, stats_text(uid), InlineKeyboardMarkup([HOME_ROW]))
        return
    if data == "menu_lang":
        await show(query, context, LANG_PICK_TEXT, lang_pick_kb())
        return
    if data == "faq":
        await show(query, context, faq.faq_text(lang), faq.faq_kb(lang))
        return
    if data.startswith("faq:"):
        try:
            i = int(data.split(":", 1)[1])
        except ValueError:
            return
        await show(query, context, faq.answer_text(lang, i), faq.answer_kb(lang, i))
        return

    if await referral.handle_callback(query, context, uid, data):
        return
    if await admin.handle_admin_callback(query, context, uid, data):
        return
    if await billing.handle_billing_callback(query, context, uid, data):
        return
    if await task_ui.handle_task_callback(query, context, uid, data):
        return
    logger.info(f"Unknown callback: {data}")


# =============================================================================
# JOBS — sab raat 12 baje (IST)
# =============================================================================
async def midnight_job(context: ContextTypes.DEFAULT_TYPE):
    """Raat 12 baje: plan jaldi khatam / khatam ho gaya — user ko yaad, admin ko khabar."""
    for uid, exp in users_expiring_soon(48):
        if is_admin(uid):
            continue
        set_remind_stage(uid, 1)
        lang = get_lang(uid)
        trial = (get_user(uid) or {}).get("is_trial")
        what = "trial" if trial else "plan"
        await dm_user(context.bot, uid,
                      tr(lang, f"⏳ <b>Your {what} ends soon</b>\n📅 {fmt_date(exp)}\n\n"
                               "Renew now from /plan — remaining days are added, nothing is lost.",
                         f"⏳ <b>Aapka {what} jaldi khatam hone wala hai</b>\n📅 {fmt_date(exp)}\n\n"
                         "/plan se abhi renew kar lein — bache din jud jayenge."),
                      parse_mode=ParseMode.HTML,
                      reply_markup=InlineKeyboardMarkup([[btn("💎 Renew — Buy Now", BLUE, callback_data="open_plan")]]))
    expired = []
    for uid, exp in users_just_expired(3):
        if is_admin(uid):
            continue
        set_remind_stage(uid, 2)
        expired.append(uid)
        lang = get_lang(uid)
        await dm_user(context.bot, uid,
                      tr(lang, "⌛ <b>Your plan has ended.</b>\nPosts will not go out now. Renew from /plan "
                               "and continue 🚀",
                         "⌛ <b>Aapka plan khatam ho gaya.</b>\nAb posts nahi jayengi. /plan se renew karein "
                         "aur phir se shuru ho jayein 🚀"),
                      parse_mode=ParseMode.HTML,
                      reply_markup=InlineKeyboardMarkup([[btn("💎 Buy Now", BLUE, callback_data="open_plan")]]))
    if expired:
        await notify_admins(context.bot, "⌛ <b>Plan khatam</b> (aaj raat):\n" +
                            "\n".join("• " + who(u) for u in expired[:30]))


async def daily_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Raat 12 baje ke baad: har user ko pichle din ki report — kis task se kitni post."""
    end = day_start_naive()
    start = end - dt.timedelta(days=1)
    day_label = fmt_date(start).split(",")[0]
    rows = day_report_rows(start, end)
    sent = 0
    for uid, by_task in rows.items():
        lang = get_lang(uid)
        tasks = {t["id"]: t for t in list_tasks(uid)}
        total = sum(a + o for a, o in by_task.values())
        lines = [tr(lang, f"📊 <b>Daily Report — {day_label}</b>\n",
                    f"📊 <b>Din ki Report — {day_label}</b>\n"),
                 tr(lang, f"📤 Total posts: <b>{total}</b>", f"📤 Kul post: <b>{total}</b>")]
        lim = limits(uid)
        cap = "∞" if is_admin(uid) else lim.get("daily", 0)
        order = list(tasks) + [tid for tid in by_task if tid not in tasks]
        for tid in order:
            amz, other = by_task.get(tid, (0, 0))
            t = tasks.get(tid)
            if t:
                c = t["cfg"]
                name = esc(task_ui.tname(t, lang))
                dest = chan(c.get("channel_title"), c.get("channel_username"), esc(str(c.get("channel") or "—")))
                head = f"\n📋 <b>{name}</b> → 📢 {dest}"
            else:
                head = tr(lang, "\n📋 <b>Deleted task</b>", "\n📋 <b>Delete hua task</b>")
            lines.append(head)
            lines.append(tr(lang, f"     {amz + other} / {cap}  (🛍️ Amazon {amz}, 📝 other {other})",
                            f"     {amz + other} / {cap}  (🛍️ Amazon {amz}, 📝 baaki {other})"))
        lines.append(tr(lang, "\n<i>New day, new limit — happy posting! 🚀</i>",
                        "\n<i>Naya din, nayi limit — posting jaari rakhein! 🚀</i>"))
        if await dm_user(context.bot, uid, "\n".join(lines), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True):
            sent += 1
        await asyncio.sleep(0.05)
    logger.info(f"Daily report: {sent}/{len(rows)} users ko bheji")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    """Koi bhi anjaan gadbad — Railway logs mein poori detail."""
    logger.error("Handler error", exc_info=context.error)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    n = cache_cleanup(CACHE_KEEP_DAYS)
    post_log_cleanup(120)
    cleanup_old_entries()
    logger.info(f"Cleanup: {n} purane cache products hataye")


# =============================================================================
# MAIN
# =============================================================================
# Menu mein wahi jo user baar-baar chalata hai — upar sabse zyada kaam wale.
# /cancel menu mein nahi (kaam karta hai, bas dikhta nahi).
USER_COMMANDS = [
    ("start", "🏠 Home"), ("tasks", "📋 Tasks & Settings"), ("config", "⚙️ All settings at a glance"),
    ("stats", "📊 Today's posts"), ("plan", "💎 Plans & Renew"), ("refer", "🎁 Refer & Earn"),
    ("faq", "❓ FAQ"), ("help", "📖 Help"), ("language", "🌐 Language"), ("paysupport", "🧾 Payment Support"),
]
ADMIN_COMMANDS = [("admin", "👑 Admin panel"), ("user", "🔍 User detail"), ("broadcast", "📣 Broadcast"),
                  ("withdrawals", "💸 Payout requests")]


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    if not OWNER_ID:
        raise ValueError("ADMIN_ID environment variable set nahi hai ya galat hai!")

    init_db()
    referral.init_tables()
    if not get_user(OWNER_ID):          # sirf pehli baar — warna har restart pe naam mit jaata
        upsert_user(OWNER_ID, "", "Admin")

    async def post_init(app):
        try:
            await billing.start_web_server(app)
        except Exception as e:
            logger.error(f"Web server start nahi hua: {e}")
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in USER_COMMANDS])
            for aid in ADMIN_IDS:
                try:
                    await app.bot.set_my_commands([BotCommand(c, d) for c, d in USER_COMMANDS + ADMIN_COMMANDS],
                                                  scope=BotCommandScopeChat(chat_id=aid))
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Commands set nahi hui: {e}")

    async def post_shutdown(app):
        await billing.stop_web_server(app)

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter(overall_max_rate=25, overall_time_period=1,
                                     group_max_rate=19, group_time_period=60, max_retries=3))
        .concurrent_updates(True)
        .connect_timeout(20).read_timeout(30).write_timeout(60).pool_timeout(20)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    dm = filters.ChatType.PRIVATE

    # Payments sabse pehle — warna DM handler pakad leta
    app.add_handler(PreCheckoutQueryHandler(billing.handle_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, billing.handle_successful_payment))

    user_cmds = [
        ("start", cmd_start), ("menu", cmd_start), ("tasks", cmd_tasks), ("settings", cmd_tasks),
        ("config", cmd_config), ("refer", cmd_refer), ("referral", cmd_refer),
        ("help", cmd_help), ("faq", cmd_faq), ("stats", cmd_stats), ("language", cmd_language),
        ("cancel", cmd_cancel), ("plan", billing.cmd_plan), ("paysupport", billing.cmd_paysupport),
    ]
    for name, fn in user_cmds:
        app.add_handler(CommandHandler(name, user_command(fn), filters=dm))

    for name, fn in [("admin", admin.cmd_admin), ("user", admin.cmd_user), ("broadcast", admin.cmd_broadcast),
                     ("withdrawals", referral.cmd_withdrawals)]:
        app.add_handler(CommandHandler(name, admin_command(fn), filters=dm))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & dm & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT,
        handle_private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))
    app.add_error_handler(on_error)

    jq = app.job_queue
    if jq:
        jq.run_daily(midnight_job, time=dt.time(0, 0, 30, tzinfo=LOCAL_TZ), name="midnight")
        jq.run_daily(daily_report_job, time=dt.time(0, 2, 0, tzinfo=LOCAL_TZ), name="daily_report")
        jq.run_repeating(cleanup_job, interval=6 * 3600, first=300, name="cleanup")
    else:
        logger.error("JobQueue nahi mili! requirements.txt mein python-telegram-bot[job-queue] chahiye.")

    logger.info(f"{BOT_NAME} start ho raha hai...")
    app.run_polling(
        drop_pending_updates=False,
        allowed_updates=["message", "channel_post", "callback_query",
                         "my_chat_member", "pre_checkout_query"],
    )


if __name__ == "__main__":
    main()

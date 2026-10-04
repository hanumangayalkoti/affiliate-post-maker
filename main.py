"""
Deal Post Maker Bot — public version of DealsKoti Master Bot.

Koi bhi user apna Amazon affiliate tag aur channel set karke Amazon links ko
sundar deal posts mein badal sakta hai. Plan paid hai (Razorpay / Telegram
Stars), admin ke paas poora control hai.
"""
import os

# Railway template mein jo value nahi bhari ("PASTE_HERE") use khaali maano —
# baaki saari files import hone se PEHLE, kyunki wo env yahin se padhti hain.
for _k, _v in list(os.environ.items()):
    if _v.strip().upper() in ("PASTE_HERE", "PASTE HERE", "CHANGE_ME"):
        del os.environ[_k]

import time
import logging
import html as html_lib
from functools import partial

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat,
)
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
import price_watch
import settings_ui
from database import (
    queue_users, cache_cleanup, post_log_cleanup, cleanup_old_entries, user_stats,
)
from engine import (
    process_and_post, flush_queue, is_own_message, extract_urls, hidden_link_urls, get_amazon_urls, remember_own, dm_user, chat_matches,
    next_hour_delay, fmt_date, fmt_short, day_start_naive, setup_problems, SELF_MARKER,
    DAILY_POST_LIMIT,
)
from storage import init_db, load_config, find_users_by_source, find_users_by_post_channel
from users import (
    ADMIN_IDS, OWNER_ID, is_admin, is_active, is_blocked, get_user, upsert_user,
    days_left, users_expiring_soon, users_just_expired, set_remind_stage,
)

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_NAME = os.getenv("BOT_NAME", "Deal Post Maker")
CACHE_KEEP_DAYS = int(os.getenv("CACHE_KEEP_DAYS", "5"))
esc = html_lib.escape

_inactive_notice: dict = {}     # draft channel owner ko baar-baar "plan khatam" na bole
_new_users: set = set()         # abhi-abhi pehli baar aaye users (welcome / admin ko khabar)


# =============================================================================
# MENUS & TEXTS
# =============================================================================
def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚙️ Settings", callback_data="set_home"),
         InlineKeyboardButton("💳 Plan", callback_data="open_plan")],
        [InlineKeyboardButton("📊 Stats", callback_data="menu_stats"),
         InlineKeyboardButton("📉 Price Alerts", callback_data="menu_alerts")],
        [InlineKeyboardButton("❓ Help", callback_data="menu_help")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("👑 Admin Panel", callback_data="adm_home")])
    return InlineKeyboardMarkup(rows)


def welcome_text(uid: int, first_name: str) -> str:
    cfg = load_config(uid)
    u = get_user(uid) or {}
    tag_ok = bool((cfg.get("tag") or "").strip())
    ch_ok = bool(str(cfg.get("channel") or "").strip())
    if is_admin(uid):
        plan_line = "3️⃣ 💳 Plan — 👑 Admin (hamesha chalu)"
    elif is_active(uid, u):
        plan_line = f"3️⃣ 💳 Plan — ✅ chalu ({days_left(u):.0f} din baaki)"
    else:
        plan_line = f"3️⃣ 💳 Plan — ❌ /plan (₹{billing.PRICE_INR} / {billing.PLAN_DAYS} din)"

    ready = tag_ok and ch_ok and is_active(uid, u)
    return (
        f"👋 <b>Namaste {esc(first_name or 'dost')}!</b>\n\n"
        f"Main <b>{esc(BOT_NAME)}</b> hoon. Mujhe Amazon ka link bhejo — main photo, price, "
        f"discount ke saath ek sundar deal post bana ke <b>tumhare channel</b> pe daal dunga, "
        f"<b>tumhare affiliate tag</b> ke saath.\n\n"
        f"<b>Shuru karne ke liye 3 kaam:</b>\n"
        f"1️⃣ 🏷️ Affiliate tag — {'✅ ' + esc(cfg.get('tag')) if tag_ok else '❌ /tag'}\n"
        f"2️⃣ 📢 Channel — {'✅ ' + esc(cfg.get('channel_title') or cfg.get('channel')) if ch_ok else '❌ mujhe channel mein admin banao'}\n"
        f"{plan_line}\n\n"
        + ("🚀 <b>Sab ready hai!</b> Bas Amazon link bhejo." if ready
           else "Neeche ke buttons se setup karo 👇")
    )


def help_text(uid: int) -> str:
    t = (
        f"📖 <b>{esc(BOT_NAME)} — Kaise use karein</b>\n\n"
        "<b>Post kaise karein:</b>\n"
        "• Mujhe Amazon link bhejo (ek message mein kai link bhi chalenge — har product "
        "ki alag post)\n"
        "• Ya ek draft channel bana ke usme deals daalo — main khud utha lunga\n\n"
        "<b>Commands:</b>\n"
        "🏠 /start ya /menu — Main menu\n"
        "⚙️ /settings — Saari settings\n"
        "🏷️ /tag — Affiliate tag\n"
        "📢 /channel — Post channel\n"
        "📥 /draft — Draft channel (optional)\n"
        "🛍️ /amz_post — Post mein kya-kya dikhe\n"
        "🎛️ /setbutton — Post ke neeche buttons\n"
        "🔝 /header  🔚 /footer — Upar/neeche ki line\n"
        "🖼️ /watermark — Photo pe tumhara naam\n"
        "🔔 /silent — Notification silent/loud\n"
        "🅿️ /park_post — Har ghante ek saath post\n"
        "📋 /queue — Queue dekho\n"
        "📉 /track — Product ka price track karo\n"
        "🔔 /alerts — Tracked products\n"
        "📊 /stats — Tumhari posts ki ginti\n"
        "📊 /status — Saari settings ek nazar mein\n"
        "💳 /plan — Plan / payment\n"
        "🧾 /paysupport — Payment mein dikkat\n"
        "❌ /cancel — Chalu kaam band karo\n"
    )
    if DAILY_POST_LIMIT > 0:
        t += f"\n<i>Ek din mein max {DAILY_POST_LIMIT} posts.</i>\n"
    s = billing.support_line()
    if s:
        t += "\n" + s
    if is_admin(uid):
        t += ("\n\n👑 <b>Admin:</b>\n/admin — Panel\n/user ID — User detail\n"
              "/adddays ID DIN — Din jodo\n/cutdays ID DIN — Din kaato\n"
              "/block ID  /unblock ID\n/broadcast — Sabko message\n"
              "/users — Naye users\n/payments — Payments\n/testamz — Amazon API test")
    return t


def stats_text(uid: int) -> str:
    st = user_stats(uid, day_start_naive())
    lines = [
        "📊 <b>Tumhari Posts</b>\n",
        f"📅 Aaj      : <b>{st['today']}</b>",
        f"🗓️ 7 din    : <b>{st['week']}</b>",
        f"📆 30 din   : <b>{st['month']}</b>  (Amazon {st['amazon_month']}, other {st['other_month']})",
        f"🏆 Total    : <b>{st['total']}</b>",
    ]
    if st["recent"]:
        lines.append("\n<b>Latest Amazon posts:</b>")
        for title, at in st["recent"]:
            lines.append(f"• {esc((title or '')[:45])} — {fmt_short(at)}")
    lines.append("\n<i>Clicks aur kamai Amazon Associates dashboard mein dikhegi.</i>")
    return "\n".join(lines)


# =============================================================================
# GATE
# =============================================================================
def _touch(update: Update) -> int:
    u = update.effective_user
    if not u:
        return 0
    if upsert_user(u.id, u.username or "", u.first_name or ""):
        _new_users.add(u.id)
    return u.id


def user_command(fn, need_plan: bool = False):
    """Har user command ke aage: register, block check, (plan check)."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = _touch(update)
        if not uid:
            return
        if is_blocked(uid):
            await update.message.reply_text("⛔ Tumhara access band kar diya gaya hai.\n"
                                            + billing.support_line())
            return
        # Koi bhi command = pichla adhoora sawaal (tag bhejo / text bhejo...) khatam
        context.user_data.pop("action", None)
        if need_plan and not is_active(uid):
            await _need_plan(update.message.reply_text, uid)
            return
        await fn(update, context, uid)
    return wrapper


def admin_command(fn):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        if not u or not is_admin(u.id):
            return
        context.user_data.pop("action", None)
        await fn(update, context, u.id)
    return wrapper


async def _need_plan(reply, uid: int):
    u = get_user(uid) or {}
    msg = ("⌛ <b>Tumhara plan khatam ho gaya hai.</b>" if u.get("expires_at")
           else "💳 <b>Post karne ke liye plan chahiye.</b>")
    await reply(msg + f"\n\n₹{billing.PRICE_INR} mein {billing.PLAN_DAYS} din — sab features ke saath.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Plan dekho",
                                                                         callback_data="open_plan")]]))


# =============================================================================
# BASIC COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data.pop("action", None)
    user = update.effective_user
    if uid in _new_users and not is_admin(uid):
        _new_users.discard(uid)
        for aid in ADMIN_IDS:
            await dm_user(context.bot, aid,
                          f"🆕 Naya user: {esc(user.first_name or '')}"
                          f"{' @' + esc(user.username) if user.username else ''} "
                          f"<code>{uid}</code>", parse_mode=ParseMode.HTML)
    await update.message.reply_text(welcome_text(uid, user.first_name), parse_mode=ParseMode.HTML,
                                    reply_markup=main_menu_kb(uid), disable_web_page_preview=True)


async def cmd_help(update, context, uid):
    await update.message.reply_text(help_text(uid), parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def cmd_stats(update, context, uid):
    await update.message.reply_text(stats_text(uid), parse_mode=ParseMode.HTML)


async def cmd_cancel(update, context, uid):
    context.user_data.clear()
    await update.message.reply_text("❌ Band kar diya.")


LINK_ACTIONS = {"wait_channel_id", "wait_source_id", "sb_wait_link", "wait_track",
                "adm_wait_bc", "adm_wait_msg"}


def _has_amazon_link(msg) -> bool:
    text = (msg.text or msg.caption or "")
    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    return bool(get_amazon_urls(extract_urls(text) + hidden_link_urls(ents)))


# =============================================================================
# DM MESSAGES — deal ya kisi setting ka jawab
# =============================================================================
async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    uid = _touch(update)
    if is_blocked(uid):
        await msg.reply_text("⛔ Tumhara access band kar diya gaya hai.\n" + billing.support_line())
        return

    action = (context.user_data or {}).get("action")
    # Bot ne kuch aur poocha tha (tag / naam / text) par user ne deal bhej di —
    # sawaal chhodo, deal post karo. Jin sawaalon ka jawab link hi hai unhe nahi chhedte.
    if action and action not in LINK_ACTIONS and _has_amazon_link(msg):
        context.user_data.pop("action", None)
        action = None
    if action:
        if await admin.handle_admin_input(update, context, uid, action):
            return
        if action == "wait_track":
            if not is_active(uid):
                context.user_data.pop("action", None)
                await _need_plan(msg.reply_text, uid)
                return
            await price_watch.handle_track_input(update, context, uid)
            return
        if await settings_ui.handle_settings_input(update, context, uid, action):
            return
        context.user_data.pop("action", None)

    if not is_active(uid):
        await _need_plan(msg.reply_text, uid)
        return

    async def notify(text, **kwargs):
        try:
            return await msg.reply_text(text, **kwargs)
        except Exception as e:
            logger.error(f"DM reply fail: {e}")
            return None

    await process_and_post(context, uid, msg, notify, allow_park=False)


# =============================================================================
# DRAFT CHANNEL POSTS
# =============================================================================
async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg or is_own_message(msg, context.bot.id):
        return

    owners = find_users_by_source(msg.chat.id, msg.chat.username or "")
    if not owners:
        return

    for uid in owners[:1]:          # ek draft channel = ek owner
        cfg = load_config(uid)
        if not chat_matches(msg.chat, cfg.get("source_channel")):
            continue
        if is_blocked(uid):
            return
        if not is_active(uid):
            last = _inactive_notice.get(uid, 0)
            if time.time() - last > 6 * 3600:
                _inactive_notice[uid] = time.time()
                await dm_user(context.bot, uid,
                              "⌛ Draft channel mein deal aayi, par tumhara plan khatam hai — "
                              "post nahi ki. /plan se renew karo.")
            return

        src_name   = msg.chat.title or cfg.get("source_title") or "Draft"
        source_tag = f"\n📥 Source: <b>{esc(src_name)}</b>"

        async def notify(text, _uid=uid, **kwargs):
            try:
                sent = await msg.reply_text(text + SELF_MARKER, disable_notification=True, **kwargs)
                return remember_own(sent)
            except Exception as e:
                logger.error(f"Draft reply fail: {e} — DM pe bhej raha hoon")
                return await dm_user(context.bot, _uid, text, **kwargs)

        await process_and_post(context, uid, msg, notify, cfg=cfg,
                               source_tag=source_tag, allow_park=True)


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
        cfg = load_config(adder)
        if chat_matches(chat, cfg.get("channel")) or chat_matches(chat, cfg.get("source_channel")):
            return
        note = "" if getattr(new, "can_post_messages", False) else \
            "\n\n⚠️ <i>'Post Messages' permission band hai — post channel ke liye ON karna padega.</i>"
        await dm_user(
            context.bot, adder,
            f"📢 Tumne mujhe <b>{esc(chat.title or 'channel')}</b> mein admin banaya!\n\n"
            f"Is channel ka kya karna hai?" + note,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Isme deals post karo", callback_data=f"chset_post_{chat.id}")],
                [InlineKeyboardButton("📥 Ye mera draft channel hai", callback_data=f"chset_src_{chat.id}")],
                [InlineKeyboardButton("❌ Kuch nahi", callback_data="cancel")],
            ]))
        return

    if new.status in ("left", "kicked", "member", "restricted"):
        for uid in find_users_by_post_channel(chat.id, chat.username or ""):
            await dm_user(context.bot, uid,
                          f"⚠️ Mujhe <b>{esc(chat.title or 'tumhare channel')}</b> se hata diya gaya "
                          f"(ya admin nahi raha). Ab wahan post nahi ho payegi.\n"
                          f"Dobara admin banao ya /channel se naya channel set karo.",
                          parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    uid = query.from_user.id
    data = query.data or ""

    if is_blocked(uid):
        await query.answer("⛔ Access band hai.", show_alert=True)
        return
    if not get_user(uid):
        upsert_user(uid, query.from_user.username or "", query.from_user.first_name or "")

    if data.startswith("adm_"):
        await admin.handle_admin_callback(query, context, uid, data)
        try:
            await query.answer()
        except Exception:
            pass
        return

    try:
        await query.answer()
    except Exception:
        pass

    if data == "cancel":
        context.user_data.clear()
        try:
            await query.edit_message_text("❌ Band kar diya.")
        except Exception:
            pass
        return

    if data == "menu_home":
        try:
            await query.edit_message_text(welcome_text(uid, query.from_user.first_name),
                                          parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(uid))
        except Exception:
            pass
        return
    if data == "menu_help":
        await query.message.reply_text(help_text(uid), parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        return
    if data == "menu_stats":
        await query.message.reply_text(stats_text(uid), parse_mode=ParseMode.HTML)
        return
    if data == "menu_alerts":
        await query.message.reply_text(price_watch.alerts_text(uid), parse_mode=ParseMode.HTML,
                                       reply_markup=price_watch.alerts_kb(uid),
                                       disable_web_page_preview=True)
        return

    if data.startswith("chset_"):
        _, kind, cid = data.split("_", 2)
        try:
            cid_int = int(cid)
        except ValueError:
            return
        if kind == "post":
            out = await settings_ui.save_post_channel(context.bot, uid, cid_int)
        else:
            out = await settings_ui.save_source_channel(context.bot, uid, cid_int)
        try:
            await query.edit_message_text(out, parse_mode=ParseMode.HTML)
        except Exception:
            await query.message.reply_text(out, parse_mode=ParseMode.HTML)
        if out.startswith("✅"):
            probs = setup_problems(load_config(uid))
            if probs:
                await query.message.reply_text("Bas ye baaki hai:\n" + "\n".join(probs))
        return

    if await billing.handle_billing_callback(query, context, uid, data):
        return
    if data.startswith("pw_"):
        await price_watch.handle_watch_callback(query, context, uid, data)
        return
    if await settings_ui.handle_settings_callback(query, context, uid, data):
        return
    logger.info(f"Unknown callback: {data}")


# =============================================================================
# JOBS
# =============================================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    """Har ghante — jinki queue mein kuch hai unki batch bhejo (sab parallel)."""
    for uid in queue_users():
        context.application.create_task(flush_queue(context.application, uid, reason="hourly"))


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Plan khatam hone se pehle aur baad mein yaad dilao (har user ko ek-ek baar)."""
    for uid, exp in users_expiring_soon(48):
        if is_admin(uid):
            continue
        set_remind_stage(uid, 1)
        await dm_user(context.bot, uid,
                      f"⏳ <b>Tumhara plan jaldi khatam hone wala hai</b>\n"
                      f"📅 {fmt_date(exp)}\n\n/plan se abhi renew kar lo — bache hue din jud jaayenge.",
                      parse_mode=ParseMode.HTML,
                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Renew karo",
                                                                               callback_data="open_plan")]]))
    for uid, exp in users_just_expired(3):
        if is_admin(uid):
            continue
        set_remind_stage(uid, 2)
        await dm_user(context.bot, uid,
                      "⌛ <b>Tumhara plan khatam ho gaya.</b>\nAb posts nahi jaayengi. "
                      "/plan se renew karo aur phir se shuru ho jao 🚀",
                      parse_mode=ParseMode.HTML,
                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Renew karo",
                                                                               callback_data="open_plan")]]))


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE):
    n = cache_cleanup(CACHE_KEEP_DAYS)
    post_log_cleanup(120)
    cleanup_old_entries()
    logger.info(f"Cleanup: {n} purane cache products hataye")


# =============================================================================
# MAIN
# =============================================================================
USER_COMMANDS = [
    ("start", "🏠 Main menu"), ("settings", "⚙️ Saari settings"), ("tag", "🏷️ Affiliate tag"),
    ("channel", "📢 Post channel"), ("draft", "📥 Draft channel"), ("plan", "💳 Plan / payment"),
    ("amz_post", "🛍️ Post mein kya dikhe"), ("setbutton", "🎛️ Post ke buttons"),
    ("header", "🔝 Upar ki line"), ("footer", "🔚 Neeche ki line"),
    ("watermark", "🖼️ Photo pe naam"), ("silent", "🔔 Silent / loud"),
    ("park_post", "🅿️ Har ghante batch"), ("queue", "📋 Queue"),
    ("track", "📉 Price track karo"), ("alerts", "🔔 Tracked products"),
    ("stats", "📊 Meri posts"), ("status", "📊 Settings ek nazar mein"),
    ("help", "📖 Madad"), ("paysupport", "🧾 Payment support"), ("cancel", "❌ Band karo"),
]
ADMIN_COMMANDS = [
    ("admin", "👑 Admin panel"), ("user", "🔍 User detail"), ("adddays", "➕ Din jodo"),
    ("cutdays", "➖ Din kaato"), ("block", "⛔ Block"), ("unblock", "✅ Unblock"),
    ("broadcast", "📣 Sabko message"), ("users", "🆕 Naye users"),
    ("payments", "💰 Payments"), ("testamz", "🧪 Amazon API test"),
]


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    if not OWNER_ID:
        raise ValueError("ADMIN_ID environment variable set nahi hai ya galat hai!")

    init_db()
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
                    await app.bot.set_my_commands(
                        [BotCommand(c, d) for c, d in USER_COMMANDS + ADMIN_COMMANDS],
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
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    dm = filters.ChatType.PRIVATE

    # Payments sabse pehle — warna DM handler pakad leta
    app.add_handler(PreCheckoutQueryHandler(billing.handle_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, billing.handle_successful_payment))

    user_cmds = [
        ("start", cmd_start, False), ("menu", cmd_start, False), ("help", cmd_help, False),
        ("cancel", cmd_cancel, False), ("stats", cmd_stats, False),
        ("settings", settings_ui.cmd_settings, False), ("status", settings_ui.cmd_status, False),
        ("tag", settings_ui.cmd_tag, False),
        ("channel", settings_ui.cmd_channel, False), ("setchannel", settings_ui.cmd_channel, False),
        ("draft", settings_ui.cmd_source, False), ("setsource", settings_ui.cmd_source, False),
        ("amz_post", settings_ui.cmd_amz_post, False), ("park_post", settings_ui.cmd_park_post, False),
        ("queue", settings_ui.cmd_queue, False), ("silent", settings_ui.cmd_silent, False),
        ("header", settings_ui.cmd_header, False), ("footer", settings_ui.cmd_footer, False),
        ("watermark", settings_ui.cmd_watermark, False), ("setbutton", settings_ui.cmd_setbutton, False),
        ("exportconfig", settings_ui.cmd_exportconfig, False),
        ("plan", billing.cmd_plan, False), ("paysupport", billing.cmd_paysupport, False),
        ("track", price_watch.cmd_track, True), ("alerts", price_watch.cmd_alerts, False),
    ]
    for name, fn, need_plan in user_cmds:
        app.add_handler(CommandHandler(name, user_command(fn, need_plan), filters=dm))

    admin_cmds = [
        ("admin", admin.cmd_admin), ("user", admin.cmd_user),
        ("adddays", admin.cmd_adddays), ("cutdays", partial(admin.cmd_adddays, sign=-1)),
        ("block", admin.cmd_block), ("unblock", partial(admin.cmd_block, block=False)),
        ("broadcast", admin.cmd_broadcast), ("users", admin.cmd_users),
        ("payments", admin.cmd_payments), ("testamz", admin.cmd_testamz),
    ]
    for name, fn in admin_cmds:
        app.add_handler(CommandHandler(name, admin_command(fn), filters=dm))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & dm & ~filters.COMMAND & ~filters.SUCCESSFUL_PAYMENT,
        handle_private))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_channel_post))

    jq = app.job_queue
    if jq:
        jq.run_repeating(hourly_job, interval=3600, first=next_hour_delay(), name="hourly_flush")
        jq.run_repeating(price_watch.price_check_job, interval=price_watch.PRICE_CHECK_MINUTES * 60,
                         first=120, name="price_check")
        jq.run_repeating(reminder_job, interval=1800, first=60, name="reminders")
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

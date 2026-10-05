"""
admin.py — sirf admin ke liye: stats, user dhoondo, din jodo/kaato, block,
broadcast, payments, Amazon API test.
"""
import asyncio
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes

from amazon_api import get_product_by_asin, api_stats
from caption import FIELD_LABELS
from database import global_post_stats, cache_count, user_stats
from engine import fmt_date, day_start_naive, dm_user
from storage import load_config
from users import (
    find_user, get_user, add_days, end_plan, set_blocked, is_active, days_left,
    user_counts, recent_users, list_user_ids, revenue_summary, recent_payments,
    mark_bot_blocked, is_admin,
)

logger = logging.getLogger(__name__)
esc = html_lib.escape

_broadcast_running = {"on": False}


def _who(u: dict) -> str:
    if not u:
        return "?"
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f"{name}{un}".strip() or str(u.get("user_id"))


# =============================================================================
# PANEL
# =============================================================================
def admin_home_text() -> str:
    c = user_counts()
    p = global_post_stats(day_start_naive())
    r = revenue_summary(day_start_naive())
    a = api_stats()
    return (
        "👑 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{c['total']}</b>  (aaj naye: {c['new_today']})\n"
        f"✅ Plan chalu: <b>{c['active']}</b>\n"
        f"⌛ Plan khatam: {c['expired']}   🆕 Kabhi nahi liya: {c['never_paid']}\n"
        f"⛔ Blocked: {c['blocked']}   🚫 Bot block kiya: {c['bot_blocked']}\n\n"
        f"📤 Posts aaj: <b>{p['today']}</b> ({p['active_posters_today']} users)  |  30 din: {p['month']}\n\n"
        f"💰 30 din: <b>₹{r['inr_month']}</b> + {r['stars_month']}⭐  ({r['paid_month']} payments)\n"
        f"💰 Total : ₹{r['inr_total']} + {r['stars_total']}⭐\n\n"
        f"🗄️ Product cache: {cache_count()}  |  API calls (restart se): {a['calls']}, "
        f"cache se mile: {a['cache_hits']}"
    )


def admin_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 User dhoondo", callback_data="adm_find"),
         InlineKeyboardButton("🆕 Naye users", callback_data="adm_recent")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="adm_bc"),
         InlineKeyboardButton("💰 Payments", callback_data="adm_pays")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm_home"),
         InlineKeyboardButton("❌ Close", callback_data="cancel")],
    ])


def user_card_text(u: dict) -> str:
    uid = u["user_id"]
    cfg = load_config(uid)
    st = user_stats(uid, day_start_naive())
    if is_admin(uid):
        plan = "👑 Admin"
    elif u.get("blocked"):
        plan = "⛔ BLOCKED"
    elif is_active(uid, u):
        plan = f"✅ Chalu — {days_left(u):.1f} din baaki"
    elif u.get("expires_at"):
        plan = "⌛ Khatam"
    else:
        plan = "🆕 Kabhi nahi liya"
    return (
        f"👤 <b>{_who(u)}</b>\n🆔 <code>{uid}</code>\n\n"
        f"💳 Plan: <b>{plan}</b>\n"
        f"📅 Expiry: {fmt_date(u.get('expires_at'))}\n"
        f"🗓️ Joined: {fmt_date(u.get('joined_at'))}\n"
        f"👀 Last seen: {fmt_date(u.get('last_seen'))}\n"
        f"{'🚫 Bot ko block kiya hua hai' + chr(10) if u.get('bot_blocked') else ''}\n"
        f"🏷️ Tag: <code>{esc(cfg.get('tag') or '—')}</code>\n"
        f"📢 Channel: {esc(cfg.get('channel_title') or cfg.get('channel') or '—')}\n"
        f"📥 Draft: {esc(cfg.get('source_title') or cfg.get('source_channel') or '—')}\n\n"
        f"📤 Posts: aaj {st['today']} | 7 din {st['week']} | total {st['total']}"
    )


def user_card_kb(u: dict) -> InlineKeyboardMarkup:
    uid = u["user_id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("+1 din", callback_data=f"adm_d_{uid}_1"),
         InlineKeyboardButton("+7 din", callback_data=f"adm_d_{uid}_7"),
         InlineKeyboardButton("+30 din", callback_data=f"adm_d_{uid}_30")],
        [InlineKeyboardButton("−1 din", callback_data=f"adm_d_{uid}_-1"),
         InlineKeyboardButton("−7 din", callback_data=f"adm_d_{uid}_-7"),
         InlineKeyboardButton("−30 din", callback_data=f"adm_d_{uid}_-30")],
        [InlineKeyboardButton("✏️ Custom din", callback_data=f"adm_cd_{uid}"),
         InlineKeyboardButton("⏹️ Plan khatam karo", callback_data=f"adm_end_{uid}")],
        [InlineKeyboardButton("✅ Unblock" if u.get("blocked") else "⛔ Block",
                              callback_data=f"adm_blk_{uid}"),
         InlineKeyboardButton("✉️ Message bhejo", callback_data=f"adm_msg_{uid}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"adm_u_{uid}"),
         InlineKeyboardButton("⬅️ Panel", callback_data="adm_home")],
    ])


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data.pop("action", None)
    await update.message.reply_text(admin_home_text(), parse_mode=ParseMode.HTML,
                                    reply_markup=admin_home_kb())


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    args = context.args or []
    if not args:
        context.user_data["action"] = "adm_wait_user"
        await update.message.reply_text("🔍 User ka Telegram ID ya @username bhejo:")
        return
    await _show_user(update.message.reply_text, args[0])


async def _show_user(reply, query: str):
    u = find_user(query)
    if not u:
        await reply("❌ User nahi mila. (User ne bot pe /start kiya hona chahiye.)")
        return
    await reply(user_card_text(u), parse_mode=ParseMode.HTML, reply_markup=user_card_kb(u))


async def _change_days(bot, target: int, days: float):
    new_exp = add_days(target, days)
    if new_exp is None:
        return None
    if days > 0:
        await dm_user(bot, target,
                      f"🎁 <b>Tumhare plan mein {days:g} din jod diye gaye!</b>\n"
                      f"📅 Plan ab chalega: <b>{fmt_date(new_exp)}</b> tak",
                      parse_mode=ParseMode.HTML)
    return new_exp


async def cmd_adddays(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, sign: int = 1):
    args = context.args or []
    if len(args) < 2:
        name = "adddays" if sign > 0 else "cutdays"
        await update.message.reply_text(f"Aise likho: <code>/{name} USER_ID DIN</code>\n"
                                        f"Jaise: <code>/{name} 123456789 7</code>",
                                        parse_mode=ParseMode.HTML)
        return
    u = find_user(args[0])
    if not u:
        await update.message.reply_text("❌ User nahi mila.")
        return
    try:
        days = float(args[1]) * sign
    except ValueError:
        await update.message.reply_text("⚠️ Din number mein likho.")
        return
    new_exp = await _change_days(context.bot, u["user_id"], days)
    await update.message.reply_text(
        f"✅ {_who(u)} — {days:+g} din\n📅 Nayi expiry: <b>{fmt_date(new_exp)}</b>",
        parse_mode=ParseMode.HTML)


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, block: bool = True):
    args = context.args or []
    if not args:
        await update.message.reply_text(f"Aise likho: <code>/{'block' if block else 'unblock'} USER_ID</code>",
                                        parse_mode=ParseMode.HTML)
        return
    u = find_user(args[0])
    if not u:
        await update.message.reply_text("❌ User nahi mila.")
        return
    if is_admin(u["user_id"]):
        await update.message.reply_text("⚠️ Admin ko block nahi kar sakte.")
        return
    set_blocked(u["user_id"], block)
    await update.message.reply_text(f"{'⛔ Block' if block else '✅ Unblock'} kar diya: {_who(u)}",
                                    parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data["action"] = "adm_wait_bc"
    await update.message.reply_text(
        "📣 <b>Broadcast</b>\n\nJo message sabko bhejna hai wo bhejo "
        "(text, photo, video — kuch bhi). Main pehle confirm karunga.\n\n"
        "<i>Cancel: /cancel</i>", parse_mode=ParseMode.HTML)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    await update.message.reply_text(_recent_text(), parse_mode=ParseMode.HTML)


async def cmd_payments(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    await update.message.reply_text(_payments_text(), parse_mode=ParseMode.HTML)


def _recent_text() -> str:
    rows = recent_users(20)
    if not rows:
        return "Abhi koi user nahi."
    lines = ["🆕 <b>Naye users (latest 20)</b>\n"]
    for u in rows:
        mark = "✅" if is_active(u["user_id"], u) else ("⛔" if u.get("blocked") else "⌛")
        lines.append(f"{mark} {_who(u)} — <code>{u['user_id']}</code>")
    lines.append("\n<i>Detail ke liye: /user ID</i>")
    return "\n".join(lines)


def _payments_text() -> str:
    rows = recent_payments(15)
    if not rows:
        return "💰 Abhi koi payment nahi aaya."
    lines = ["💰 <b>Latest payments</b>\n"]
    for uid, prov, amount, cur, days, paid_at in rows:
        amt = f"₹{amount // 100}" if cur == "INR" else f"{amount}⭐"
        lines.append(f"• <code>{uid}</code> — {amt} ({prov}, {days}d) — {fmt_date(paid_at)}")
    return "\n".join(lines)


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    await update.message.reply_text("🔄 Amazon API test ho rahi hai (cache skip)...")
    try:
        product = await get_product_by_asin("B08N5WRWNW", max_age_minutes=0, allow_stale=False)
        if product and product.get("title"):
            got = []
            for k in ("deal_badge", "stock_note", "seller", "brand", "sales_rank", "features"):
                if product.get(k):
                    got.append(FIELD_LABELS.get({"deal_badge": "deal", "stock_note": "stock",
                                                 "sales_rank": "rank"}.get(k, k), k))
            await update.message.reply_text(
                f"✅ <b>API kaam kar raha hai!</b>\n\n"
                f"🏷️ {esc(product['title'][:70])}\n"
                f"💰 {product.get('deal_price') or 'N/A'} (MRP {product.get('actual_price') or 'N/A'}, "
                f"{product.get('discount_pct', 0)}% off)\n"
                f"⭐ {product.get('rating') or 'N/A'} / {product.get('review_count') or 'N/A'} reviews\n"
                f"🖼️ Image: {'✅' if product.get('image_url') else '❌'}\n\n"
                f"<b>Extra fields:</b> {esc(', '.join(got)) if got else 'koi nahi'}",
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await update.message.reply_text("⚠️ Product data nahi mila.\n"
                                            "CREDENTIAL_ID / CREDENTIAL_SECRET / PARTNER_TAG check karo.")
    except Exception as e:
        await update.message.reply_text(f"❌ API error:\n<code>{esc(str(e))}</code>",
                                        parse_mode=ParseMode.HTML)


# =============================================================================
# BROADCAST
# =============================================================================
async def _run_broadcast(app, admin_uid: int, from_chat: int, msg_id: int, segment: str):
    ids = list_user_ids(segment)
    ok = fail = blocked = 0
    _broadcast_running["on"] = True
    try:
        for i, target in enumerate(ids):
            try:
                await app.bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=msg_id)
                ok += 1
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 5)) + 1)
                try:
                    await app.bot.copy_message(chat_id=target, from_chat_id=from_chat, message_id=msg_id)
                    ok += 1
                except Exception:
                    fail += 1
            except Forbidden:
                blocked += 1
                mark_bot_blocked(target)
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
            if i and i % 200 == 0:
                await dm_user(app.bot, admin_uid, f"📣 Broadcast chal raha hai... {i}/{len(ids)}")
    finally:
        _broadcast_running["on"] = False
    await dm_user(app.bot, admin_uid,
                  f"📣 <b>Broadcast poora</b>\n\n✅ Pahuncha: {ok}\n🚫 Bot block: {blocked}\n❌ Fail: {fail}",
                  parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_admin_callback(query, context, uid: int, data: str) -> bool:
    if not data.startswith("adm_"):
        return False
    if not is_admin(uid):
        await query.answer("Sirf admin ke liye.", show_alert=True)
        return True

    async def show(text, kb=None):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)
        except Exception:
            pass

    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Panel", callback_data="adm_home")]])

    if data == "adm_home":
        await show(admin_home_text(), admin_home_kb())
        return True
    if data == "adm_find":
        context.user_data["action"] = "adm_wait_user"
        await show("🔍 User ka Telegram ID ya @username bhejo:")
        return True
    if data == "adm_recent":
        await show(_recent_text(), back)
        return True
    if data == "adm_pays":
        await show(_payments_text(), back)
        return True
    if data == "adm_bc":
        context.user_data["action"] = "adm_wait_bc"
        await show("📣 Jo message sabko bhejna hai wo bhejo (text / photo / video).")
        return True

    if data.startswith("adm_bcgo_"):
        seg = data.split("_", 2)[2]
        src = context.user_data.pop("bc_src", None)
        if not src:
            await show("⚠️ Message nahi mila, /broadcast dobara karo.")
            return True
        if _broadcast_running["on"]:
            await show("⚠️ Ek broadcast pehle se chal raha hai.")
            return True
        n = len(list_user_ids(seg))
        await show(f"📣 Bhej raha hoon — {n} users. Poora hone pe summary aayegi.")
        context.application.create_task(
            _run_broadcast(context.application, uid, src[0], src[1], seg))
        return True

    parts = data.split("_")
    try:
        target = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return True

    if parts[1] == "u":
        u = get_user(target)
        if u:
            await show(user_card_text(u), user_card_kb(u))
        return True
    if parts[1] == "d" and len(parts) == 4:
        try:
            days = float(parts[3])
        except ValueError:
            return True
        await _change_days(context.bot, target, days)
        u = get_user(target)
        if u:
            await show(f"✅ {days:+g} din\n\n" + user_card_text(u), user_card_kb(u))
        return True
    if parts[1] == "cd":
        context.user_data["action"] = "adm_wait_days"
        context.user_data["adm_target"] = target
        await query.message.reply_text("✏️ Kitne din? (jodne ke liye <code>10</code>, kaatne ke liye "
                                       "<code>-10</code>)", parse_mode=ParseMode.HTML)
        return True
    if parts[1] == "end":
        end_plan(target)
        u = get_user(target)
        if u:
            await show("⏹️ Plan khatam kar diya.\n\n" + user_card_text(u), user_card_kb(u))
        return True
    if parts[1] == "blk":
        if is_admin(target):
            await query.answer("Admin ko block nahi kar sakte.", show_alert=True)
            return True
        u = get_user(target)
        if u:
            set_blocked(target, not u.get("blocked"))
            u = get_user(target)
            await show(user_card_text(u), user_card_kb(u))
        return True
    if parts[1] == "msg":
        context.user_data["action"] = "adm_wait_msg"
        context.user_data["adm_target"] = target
        await query.message.reply_text("✉️ Is user ko kya message bhejna hai? Bhejo:")
        return True
    return True


# =============================================================================
# TEXT INPUT
# =============================================================================
async def handle_admin_input(update: Update, context, uid: int, action: str) -> bool:
    if not action.startswith("adm_") or not is_admin(uid):
        return False
    msg = update.message
    text = (msg.text or "").strip()

    if action == "adm_wait_user":
        context.user_data.pop("action", None)
        await _show_user(msg.reply_text, text)
        return True

    if action == "adm_wait_days":
        target = context.user_data.get("adm_target")
        try:
            days = float(text)
        except ValueError:
            await msg.reply_text("⚠️ Sirf number bhejo, jaise 10 ya -5")
            return True
        context.user_data.pop("action", None)
        context.user_data.pop("adm_target", None)
        new_exp = await _change_days(context.bot, target, days)
        u = get_user(target)
        if u:
            await msg.reply_text(f"✅ {days:+g} din — expiry {fmt_date(new_exp)}\n\n" + user_card_text(u),
                                 parse_mode=ParseMode.HTML, reply_markup=user_card_kb(u))
        return True

    if action == "adm_wait_msg":
        target = context.user_data.pop("adm_target", None)
        context.user_data.pop("action", None)
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=msg.chat_id,
                                           message_id=msg.message_id)
            await msg.reply_text("✅ Bhej diya.")
        except Exception as e:
            await msg.reply_text(f"❌ Nahi gaya: {esc(str(e)[:100])}")
        return True

    if action == "adm_wait_bc":
        context.user_data.pop("action", None)
        context.user_data["bc_src"] = (msg.chat_id, msg.message_id)
        n_all, n_act, n_exp = (len(list_user_ids(s)) for s in ("all", "active", "expired"))
        await msg.reply_text(
            "📣 <b>Ye message kisko bhejna hai?</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"👥 Sabko ({n_all})", callback_data="adm_bcgo_all")],
                [InlineKeyboardButton(f"✅ Plan chalu wale ({n_act})", callback_data="adm_bcgo_active")],
                [InlineKeyboardButton(f"⌛ Bina plan wale ({n_exp})", callback_data="adm_bcgo_expired")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
            ]))
        return True
    return False

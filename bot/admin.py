"""
admin.py — sirf admin ke liye, sab kuch inline buttons se:
users ki list (filter + pages + 1-10 number), user card (plan, tasks, din
jodo/kaato, tier badlo, block, message), payments, broadcast, stats.

Callback:  adm:home | adm:u:<seg>:<page> | adm:v:<uid>:<seg>:<page>
           adm:d:<uid>:<days>:<seg>:<page> | adm:t:<uid>:<tier>:<seg>:<page>
           adm:e / adm:b / adm:m / adm:k / adm:tk / adm:ga / adm:rd :<uid>:<seg>:<page>
           adm:p | adm:bc | adm:bcgo:<seg> | adm:s
"""
import asyncio
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter
from telegram.ext import ContextTypes

from amazon_api import get_product_by_asin, api_stats
from database import global_post_stats, cache_count, user_stats
from engine import fmt_date, day_start_naive, dm_user
from storage import list_tasks
from tiers import TIERS, TIER_ORDER, tier_label
from ui import btn, chan, track, GREEN, RED, BLUE
from users import (
    find_user, get_user, add_days, end_plan, set_blocked, set_tier, is_active, days_left,
    user_counts, list_users_page, list_user_ids, revenue_summary, recent_payments,
    mark_bot_blocked, is_admin, get_lang, limits,
)

logger = logging.getLogger(__name__)
esc = html_lib.escape

PAGE = 10
SEGMENTS = [("all", "👥 Sab"), ("active", "✅ Paid"), ("trial", "🎁 Trial"),
            ("expired", "⌛ Khatam"), ("blocked", "⛔ Block")]
_broadcast_running = {"on": False}


def _who(u: dict) -> str:
    if not u:
        return "?"
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f"{name}{un}".strip() or str(u.get("user_id"))


def _plan_short(u: dict) -> str:
    uid = u["user_id"]
    if is_admin(uid):
        return "👑"
    if u.get("blocked"):
        return "⛔"
    if is_active(uid, u):
        t = TIERS.get(u.get("tier") or "", {})
        tag = "🎁" if u.get("is_trial") else t.get("emoji", "✅")
        return f"{tag} {days_left(u):.0f}d"
    return "⌛"


# =============================================================================
# HOME
# =============================================================================
def home_text() -> str:
    c = user_counts()
    p = global_post_stats(day_start_naive())
    r = revenue_summary(day_start_naive())
    a = api_stats()
    return (
        "👑 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{c['total']}</b>  (aaj naye: {c['new_today']})\n"
        f"✅ Paid: <b>{c['active']}</b>  (🥉 {c['basic']} · 🥈 {c['pro']} · 🥇 {c['premium']})\n"
        f"🎁 Trial: {c['trial']}   ⌛ Khatam: {c['expired']}\n"
        f"⛔ Blocked: {c['blocked']}   🚫 Bot block: {c['bot_blocked']}\n\n"
        f"📤 Posts aaj: <b>{p['today']}</b> ({p['active_posters_today']} users)  |  30 din: {p['month']}\n\n"
        f"💰 30 din: <b>₹{r['inr_month']}</b> + {r['stars_month']}⭐  ({r['paid_month']} payments)\n"
        f"💰 Total : ₹{r['inr_total']} + {r['stars_total']}⭐\n\n"
        f"🗄️ Product cache: {cache_count()}  |  Amazon API calls: {a['calls']}, cache hits: {a['cache_hits']}"
    )


def _pending_payouts() -> int:
    import referral
    return referral.count_pending_withdrawals()


def home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("👥 Users", BLUE, callback_data="adm:u:all:0"),
         btn("💰 Payments", callback_data="adm:p")],
        [btn("🔍 User dhoondo", callback_data="adm:s"),
         btn("📣 Broadcast", callback_data="adm:bc")],
        [btn(f"💸 Payout requests ({_pending_payouts()})", callback_data="adm:wd")],
        [btn("🧪 Amazon API test", callback_data="adm:api"),
         btn("🔄 Refresh", callback_data="adm:home")],
        [btn("🏠 Home", callback_data="home")],
    ])


# =============================================================================
# USERS LIST
# =============================================================================
def users_page(seg: str, page: int):
    users, total = list_users_page(seg, page * PAGE, PAGE)
    pages = max(1, (total + PAGE - 1) // PAGE)
    seg_name = dict(SEGMENTS).get(seg, seg)
    lines = [f"👥 <b>Users — {seg_name}</b>  ({total})   page {page + 1}/{pages}\n"]
    for i, u in enumerate(users, 1):
        lines.append(f"<b>{i}.</b> {_who(u)} — {_plan_short(u)}")
    if not users:
        lines.append("<i>Koi user nahi.</i>")
    lines.append("\n<i>Number dabayein — user ki poori detail.</i>")

    rows = [[btn(("✔️ " if s == seg else "") + name, GREEN if s == seg else "",
                 callback_data=f"adm:u:{s}:0") for s, name in SEGMENTS[:3]],
            [btn(("✔️ " if s == seg else "") + name, GREEN if s == seg else "",
                 callback_data=f"adm:u:{s}:0") for s, name in SEGMENTS[3:]]]
    nums = [btn(str(i), callback_data=f"adm:v:{u['user_id']}:{seg}:{page}") for i, u in enumerate(users, 1)]
    rows += [nums[i:i + 5] for i in range(0, len(nums), 5)]
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Pichla", callback_data=f"adm:u:{seg}:{page - 1}"))
    if page + 1 < pages:
        nav.append(btn("Agla ➡️", callback_data=f"adm:u:{seg}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("⬅️ Panel", callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# =============================================================================
# USER CARD
# =============================================================================
def user_card(uid: int, seg: str = "all", page: int = 0):
    u = get_user(uid)
    if not u:
        return "❌ User nahi mila.", InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]])
    st = user_stats(uid, day_start_naive())
    lim = limits(uid, u)
    if is_admin(uid):
        plan = "👑 Admin"
    elif u.get("blocked"):
        plan = "⛔ BLOCKED"
    elif is_active(uid, u):
        plan = f"{lim['emoji']} {lim['name']}{' (trial)' if u.get('is_trial') else ''} — {days_left(u):.1f} din baaki"
    elif u.get("expires_at"):
        plan = "⌛ Khatam"
    else:
        plan = "🆕 Kabhi nahi liya"
    tasks = list_tasks(uid)
    lines = [
        f"👤 <b>{_who(u)}</b>\n🆔 <code>{uid}</code>   🌐 {'English' if u.get('lang') == 'en' else 'Hinglish'}\n",
        f"💳 Plan: <b>{plan}</b>",
        f"📅 Expiry: {fmt_date(u.get('expires_at'))}",
        f"🎁 Trial liya: {'haan' if u.get('trial_used') else 'nahi'}",
        f"🗓️ Joined: {fmt_date(u.get('joined_at'))}   👀 Last seen: {fmt_date(u.get('last_seen'))}",
    ]
    if u.get("bot_blocked"):
        lines.append("🚫 Bot ko block kiya hua hai")
    import referral
    ref_line = referral.admin_referral_line(uid)
    if ref_line:
        lines.append(ref_line)
    daily = "∞" if lim.get("key") == "admin" else lim["daily"]
    lines.append(f"\n📤 Posts: aaj {st['today']} (limit {daily}/task) | 7 din {st['week']} | total {st['total']}")
    lines.append(f"\n📋 <b>Tasks ({len(tasks)})</b>")
    for t in tasks[:6]:
        c = t["cfg"]
        lines.append(f"{'⏸️' if t['paused'] else '▶️'} <b>{esc(c.get('name') or '#' + str(t['id']))}</b> — "
                     f"<code>{esc(c.get('tag') or '—')}</code>\n"
                     f"     📥 {chan(c.get('source_title'), c.get('source_username'))} ➜ "
                     f"📢 {chan(c.get('channel_title'), c.get('channel_username'))}  "
                     f"(aaj {st['by_task'].get(t['id'], 0)})")
    pays = recent_payments(3, uid)
    if pays:
        lines.append("\n💰 <b>Payments</b>")
        for _, prov, amount, cur, days, paid_at, tier in pays:
            amt = f"₹{amount // 100}" if cur == "INR" else f"{amount}⭐"
            lines.append(f"• {amt} {tier_label(tier or '')} ({prov}) — {fmt_date(paid_at)}")

    b = f"{uid}:{seg}:{page}"
    rows = [
        [btn("📋 Tasks dekho", BLUE, callback_data=f"adm:tk:{b}")],
        [btn("+1 din", GREEN, callback_data=f"adm:d:{uid}:1:{seg}:{page}"),
         btn("+7 din", GREEN, callback_data=f"adm:d:{uid}:7:{seg}:{page}"),
         btn("+30 din", GREEN, callback_data=f"adm:d:{uid}:30:{seg}:{page}")],
        [btn("−1 din", RED, callback_data=f"adm:d:{uid}:-1:{seg}:{page}"),
         btn("−7 din", RED, callback_data=f"adm:d:{uid}:-7:{seg}:{page}"),
         btn("−30 din", RED, callback_data=f"adm:d:{uid}:-30:{seg}:{page}")],
        [btn("➕ Din jodo (likh ke)", callback_data=f"adm:ga:{b}"),
         btn("➖ Din kaato (likh ke)", callback_data=f"adm:rd:{b}")],
        [btn(("✔️ " if (u.get("tier") == k and is_active(uid, u) and not u.get("is_trial")) else "")
             + tier_label(k), callback_data=f"adm:t:{uid}:{k}:{seg}:{page}") for k in TIER_ORDER],
        [btn("⏹️ Plan khatam", RED, callback_data=f"adm:e:{b}"),
         btn("✅ Unblock" if u.get("blocked") else "⛔ Block", "" if u.get("blocked") else RED,
             callback_data=f"adm:b:{b}")],
        [btn("✉️ Message bhejo", callback_data=f"adm:m:{b}"),
         btn("🔄 Refresh", callback_data=f"adm:v:{b}")],
        [btn("⬅️ List", callback_data=f"adm:u:{seg}:{page}"),
         btn("👑 Panel", callback_data="adm:home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def user_tasks_screen(bot, uid: int, seg: str = "all", page: int = 0):
    """Admin — user ke saare tasks poori detail ke saath (Draft / Destination link bhi)."""
    from alerts import _channel_link
    u = get_user(uid) or {"user_id": uid}
    tasks = list_tasks(uid)
    st = user_stats(uid, day_start_naive())
    lim = limits(uid)
    cap = "∞" if lim.get("key") == "admin" else lim.get("daily", 0)
    lines = [f"📋 <b>{_who(u)} ke Tasks</b> ({len(tasks)}/{lim.get('tasks', 0)})  🆔 <code>{uid}</code>"]
    if not tasks:
        lines.append("\nAbhi koi task nahi bana.")
    for t in tasks[:10]:
        c = t["cfg"]
        kinds = []
        if c.get("allow_amazon", True):
            kinds.append("🛍️ Amazon")
        if c.get("allow_other", True):
            kinds.append("📝 Non-Amazon")
        lines += [
            f"\n{'⏸️' if t['paused'] else '▶️'} <b>{esc(c.get('name') or '#' + str(t['id']))}</b> (#{t['id']})",
            f"🏷️ Tag: <code>{esc(c.get('tag') or '—')}</code>",
            f"📥 Draft: {await _channel_link(bot, uid, t, 'src')}",
            f"📢 Destination: {await _channel_link(bot, uid, t, 'dest')}",
            f"📤 Aaj: {st['by_task'].get(t['id'], 0)} / {cap}   •   {' + '.join(kinds) or '—'}",
            f"♻️ Duplicate: {'ON' if c.get('dup_check', True) else 'OFF'}   •   "
            f"🗓️ Bana: {fmt_date(t.get('created_at'))}",
        ]
    if len(tasks) > 10:
        lines.append(f"\n… aur {len(tasks) - 10} task")
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    kb = InlineKeyboardMarkup([
        [btn("🔄 Refresh", callback_data=f"adm:tk:{uid}:{seg}:{page}"),
         btn("⬅️ User", callback_data=f"adm:v:{uid}:{seg}:{page}")],
    ])
    return text, kb


def payments_text() -> str:
    rows = recent_payments(15)
    if not rows:
        return "💰 Abhi koi payment nahi aaya."
    lines = ["💰 <b>Latest payments</b>\n"]
    for uid, prov, amount, cur, days, paid_at, tier in rows:
        amt = f"₹{amount // 100}" if cur == "INR" else f"{amount}⭐"
        u = get_user(uid) or {"user_id": uid}
        lines.append(f"• {_who(u)} — {amt} {tier_label(tier or '')} ({prov}) — {fmt_date(paid_at)}")
    return "\n".join(lines)


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    m = await update.message.reply_text(home_text(), parse_mode=ParseMode.HTML, reply_markup=home_kb())
    track(context, m)


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    args = context.args or []
    if not args:
        context.user_data["action"] = "adm_find"
        m = await update.message.reply_text("🔍 User ka Telegram ID ya @username bhejein:")
        track(context, m)
        return
    u = find_user(args[0])
    text, kb = user_card(u["user_id"]) if u else ("❌ User nahi mila.", None)
    m = await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                        disable_web_page_preview=True)
    track(context, m)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data["action"] = "adm_bc"
    m = await update.message.reply_text(
        "📣 <b>Broadcast</b>\n\nJo message sabko bhejna hai wo bhejein (text, photo, video — kuch bhi). "
        "Bhejne se pehle main poochunga kisko bhejna hai.\n\n<i>Cancel: /cancel</i>", parse_mode=ParseMode.HTML)
    track(context, m)


async def _change_days(bot, target: int, days: float):
    new_exp = add_days(target, days)
    if new_exp is not None and days > 0:
        lang = get_lang(target)
        await dm_user(bot, target,
                      (f"🎁 <b>{days:g} days were added to your plan!</b>\n📅 Valid till: <b>{fmt_date(new_exp)}</b>"
                       if lang == "en" else
                       f"🎁 <b>Aapke plan mein {days:g} din jod diye gaye!</b>\n📅 Plan ab chalega: "
                       f"<b>{fmt_date(new_exp)}</b> tak"), parse_mode=ParseMode.HTML)
    return new_exp


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
    if not data.startswith("adm:"):
        return False
    if not is_admin(uid):
        await query.answer("Sirf admin ke liye.", show_alert=True)
        return True
    from task_ui import show

    p = data.split(":")
    act = p[1] if len(p) > 1 else "home"

    if act == "home":
        context.user_data.pop("action", None)
        await show(query, context, home_text(), home_kb())
        return True
    if act == "u":
        seg = p[2] if len(p) > 2 else "all"
        page = int(p[3]) if len(p) > 3 and p[3].isdigit() else 0
        text, kb = users_page(seg, page)
        await show(query, context, text, kb)
        return True
    if act == "p":
        await show(query, context, payments_text(),
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return True
    if act == "s":
        context.user_data["action"] = "adm_find"
        await show(query, context, "🔍 User ka Telegram ID ya @username bhejein:",
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return True
    if act == "bc":
        context.user_data["action"] = "adm_bc"
        await show(query, context, "📣 Jo message sabko bhejna hai wo bhejein (text / photo / video).",
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return True
    if act == "bcgo":
        seg = p[2] if len(p) > 2 else "all"
        src = context.user_data.pop("bc_src", None)
        if not src:
            await show(query, context, "⚠️ Message nahi mila, /broadcast dobara karein.")
            return True
        if _broadcast_running["on"]:
            await show(query, context, "⚠️ Ek broadcast pehle se chal raha hai.")
            return True
        n = len(list_user_ids(seg))
        await show(query, context, f"📣 Bhej raha hoon — {n} users. Poora hone pe summary aayegi.")
        context.application.create_task(_run_broadcast(context.application, uid, src[0], src[1], seg))
        return True
    if act == "wd":
        import referral
        rows_ = referral.list_pending_withdrawals()
        if not rows_:
            await query.answer("✅ Koi payout request pending nahi.", show_alert=True)
            return True
        await query.answer()
        for row in rows_:
            track(context, await query.message.reply_text(referral._admin_wd_text(row), parse_mode=ParseMode.HTML,
                                                          reply_markup=referral._admin_wd_kb(row[0])))
        return True
    if act == "api":
        await query.answer("🔄 Amazon API test ho raha hai...")
        product = await get_product_by_asin("B08N5WRWNW", max_age_minutes=0, allow_stale=False)
        if product and product.get("title"):
            text = (f"✅ <b>Amazon API kaam kar raha hai!</b>\n\n🏷️ {esc(product['title'][:70])}\n"
                    f"💰 {product.get('deal_price') or 'N/A'} (MRP {product.get('actual_price') or 'N/A'}, "
                    f"{product.get('discount_pct', 0)}% off)\n🖼️ Image: {'✅' if product.get('image_url') else '❌'}")
        else:
            text = "⚠️ Product data nahi mila.\nCREDENTIAL_ID / CREDENTIAL_SECRET / PARTNER_TAG check karein."
        await show(query, context, text, InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return True

    # User card wale actions — p = adm:<act>:<uid>:...:<seg>:<page>
    try:
        target = int(p[2])
    except (IndexError, ValueError):
        return True
    seg = p[-2] if len(p) >= 5 else "all"
    page = int(p[-1]) if len(p) >= 5 and p[-1].isdigit() else 0
    note = ""

    if act == "tk":
        text, kb = await user_tasks_screen(context.bot, target, seg, page)
        await show(query, context, text, kb)
        return True
    if act in ("ga", "rd"):
        context.user_data.update(action="adm_days", adm_target=target, adm_sign=1 if act == "ga" else -1,
                                 adm_back=f"{seg}:{page}")
        ask = ("➕ Kitne din <b>jodne</b> hain? Number bhejein (jaise <code>15</code>).\n"
               "<i>User ko message jayega.</i>" if act == "ga" else
               "➖ Kitne din <b>kaatne</b> hain? Number bhejein (jaise <code>5</code>).\n"
               "<i>User ko koi message nahi jayega.</i>")
        await show(query, context, ask,
                   InlineKeyboardMarkup([[btn("⬅️ Wapas", callback_data=f"adm:v:{target}:{seg}:{page}")]]))
        return True
    if act == "d" and len(p) >= 4:
        try:
            days = float(p[3])
        except ValueError:
            return True
        new_exp = await _change_days(context.bot, target, days)
        note = (f"✅ {days:+g} din — expiry {fmt_date(new_exp)}"
                + ("  (user ko bata diya)" if days > 0 else "  (user ko message nahi gaya)") + "\n\n")
    elif act == "t" and len(p) >= 4 and p[3] in TIERS:
        if set_tier(target, p[3]):
            from task_ui import enforce_task_limit
            paused = enforce_task_limit(target)
            note = f"✅ Tier: {tier_label(p[3])}" + (f" ({len(paused)} task pause)" if paused else "") + "\n\n"
    elif act == "e":
        end_plan(target)
        note = "⏹️ Plan khatam kar diya.\n\n"
    elif act == "b":
        if is_admin(target):
            await query.answer("Admin ko block nahi kar sakte.", show_alert=True)
            return True
        u = get_user(target) or {}
        set_blocked(target, not u.get("blocked"))
    elif act == "m":
        context.user_data.update(action="adm_msg", adm_target=target)
        await show(query, context, "✉️ Is user ko kya message bhejna hai? Bhejein:",
                   InlineKeyboardMarkup([[btn("⬅️ Wapas", callback_data=f"adm:v:{target}:{seg}:{page}")]]))
        return True

    text, kb = user_card(target, seg, page)
    await show(query, context, note + text, kb)
    return True


# =============================================================================
# TEXT INPUT
# =============================================================================
async def handle_admin_input(update: Update, context, uid: int, action: str) -> bool:
    if not action.startswith("adm_") or not is_admin(uid):
        return False
    msg = update.message
    text = (msg.text or "").strip()
    track(context, msg)

    if action == "adm_find":
        context.user_data.pop("action", None)
        u = find_user(text)
        out, kb = user_card(u["user_id"]) if u else ("❌ User nahi mila. (User ne /start kiya hona chahiye.)", None)
        m = await msg.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        track(context, m)
        return True

    if action == "adm_days":
        try:
            days = abs(float(text.replace("+", "").replace("-", "").strip()))
        except ValueError:
            m = await msg.reply_text("⚠️ Sirf number bhejein, jaise 15. (/cancel se band)")
            track(context, m)
            return True
        if days <= 0 or days > 3650:
            m = await msg.reply_text("⚠️ 1 se 3650 ke beech number bhejein.")
            track(context, m)
            return True
        target = context.user_data.pop("adm_target", None)
        sign = context.user_data.pop("adm_sign", 1)
        seg, _, page = (context.user_data.pop("adm_back", "all:0")).partition(":")
        context.user_data.pop("action", None)
        if target is None:
            return True
        # _change_days sirf din JODNE pe user ko batata hai, kaatne pe nahi
        new_exp = await _change_days(context.bot, target, sign * days)
        note = (f"✅ {sign * days:+g} din — expiry {fmt_date(new_exp)}"
                + ("  (user ko bata diya)" if sign > 0 else "  (user ko message nahi gaya)") + "\n\n")
        out, kb = user_card(target, seg or "all", int(page) if page.isdigit() else 0)
        m = await msg.reply_text(note + out, parse_mode=ParseMode.HTML, reply_markup=kb,
                                 disable_web_page_preview=True)
        track(context, m)
        return True

    if action == "adm_msg":
        target = context.user_data.pop("adm_target", None)
        context.user_data.pop("action", None)
        try:
            await context.bot.copy_message(chat_id=target, from_chat_id=msg.chat_id, message_id=msg.message_id)
            await msg.reply_text("✅ Bhej diya.")
        except Exception as e:
            await msg.reply_text(f"❌ Nahi gaya: {esc(str(e)[:100])}")
        return True

    if action == "adm_bc":
        context.user_data.pop("action", None)
        context.user_data["bc_src"] = (msg.chat_id, msg.message_id)
        counts = {s: len(list_user_ids(s)) for s in ("all", "paid_or_trial", "trial", "expired")}
        m = await msg.reply_text(
            "📣 <b>Ye message kisko bhejna hai?</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [btn(f"👥 Sabko ({counts['all']})", callback_data="adm:bcgo:all")],
                [btn(f"✅ Plan/Trial chalu ({counts['paid_or_trial']})", callback_data="adm:bcgo:paid_or_trial")],
                [btn(f"🎁 Sirf Trial ({counts['trial']})", callback_data="adm:bcgo:trial")],
                [btn(f"⌛ Plan khatam ({counts['expired']})", callback_data="adm:bcgo:expired")],
                [btn("❌ Cancel", callback_data="adm:home")],
            ]))
        track(context, m)
        return True
    return False

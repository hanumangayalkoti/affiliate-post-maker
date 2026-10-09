"""
admin.py — sirf admin ke liye, sab kuch inline buttons se:
users ki list (filter + pages + 1-10 number), user card (plan, tasks, din
jodo/kaato, tier badlo, block, message), payments, broadcast, stats.

Callback:  adm:home | adm:u:<seg>:<page> | adm:v:<uid>:<seg>:<page>
           adm:d:<uid>:<days>:<seg>:<page> | adm:t:<uid>:<tier>:<seg>:<page>
           adm:e / adm:b / adm:m / adm:k / adm:tk / adm:ga / adm:rd :<uid>:<seg>:<page>
           adm:p | adm:bc | adm:bcgo:<seg> | adm:s
"""
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from amazon_api import get_product_by_asin, api_stats
from database import global_post_stats, cache_count, user_stats
from engine import fmt_date, day_start_naive, dm_user
from storage import list_tasks
from tiers import TIERS, TIER_ORDER, tier_label
from ui import btn, chan, track, GREEN, RED, BLUE
import broadcasts
import gate
from tiers import TRIAL_DAYS
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


def _who(u: dict) -> str:
    if not u:
        return "?"
    name = esc(u.get("first_name") or "")
    un = f" @{esc(u['username'])}" if u.get("username") else ""
    return f"{name}{un}".strip() or str(u.get("user_id"))


def _plan_short(u: dict) -> str:
    """List ke liye: '🥈 Pro (🎁 Trial) · 3 din' / '⌛ Khatam' / '⛔ Block'."""
    uid = u["user_id"]
    if is_admin(uid):
        return "👑 Admin"
    if u.get("blocked"):
        return "⛔ Block"
    if is_active(uid, u):
        t = TIERS.get(u.get("tier") or "", {})
        name = f"{t.get('emoji', '✅')} {t.get('name', 'Plan')}"
        trial = " (🎁 Trial)" if u.get("is_trial") else ""
        return f"{name}{trial} · {days_left(u):.0f} din"
    return "⌛ Khatam" if u.get("expires_at") else "🆓 Free"


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
        [btn("↩️ Broadcast Recall", callback_data="adm:bcl"),
         btn(f"💸 Payouts ({_pending_payouts()})", callback_data="adm:wd")],
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
    rows.append([btn("🔍 ID / @username se dhoondo", callback_data="adm:s")])
    rows.append([btn("⬅️ Panel", callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# =============================================================================
# USER CARD
# =============================================================================
async def user_card(bot, uid: int, seg: str = "all", page: int = 0):
    u = get_user(uid)
    if not u:
        return "❌ User nahi mila.", InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]])
    st = user_stats(uid, day_start_naive())
    lim = limits(uid, u)
    active = is_active(uid, u)
    if is_admin(uid):
        plan = "👑 Admin"
    elif active:
        plan = f"{lim['emoji']} {lim['name']}{' (🎁 Trial)' if u.get('is_trial') else ''}"
    elif u.get("expires_at"):
        plan = f"⌛ Khatam ({TIERS.get(u.get('tier') or '', {}).get('name', '—')})"
    else:
        plan = "🆓 Kabhi plan nahi liya"
    if u.get("blocked"):
        status = "⛔ Admin ne block kiya"
    elif u.get("bot_blocked"):
        status = "🚫 User ne bot block kiya"
    elif active or is_admin(uid):
        status = "✅ Active"
    else:
        status = "⌛ Plan khatam — posts band"
    if not gate.join_enabled():
        member = "— (join zaroori nahi)"
    else:
        try:
            member = "✅ Joined" if await gate.is_joined(bot, uid, fresh=True) else "❌ Join nahi kiya"
        except Exception:
            member = "❓ Check nahi hua"
    pays_all = recent_payments(1, uid)
    if u.get("is_trial") and active:
        started = f"{fmt_date(u.get('joined_at'))} (trial)"
    elif pays_all:
        started = f"{fmt_date(pays_all[0][5])} (last payment)"
    else:
        started = "—"
    left = f"{days_left(u):.1f} din" if active else "0"
    uname = f"@{esc(u['username'])}" if u.get("username") else "— (nahi hai)"
    lines = [
        f"👤 <b>{esc(u.get('first_name') or '—')}</b>",
        f"🔗 Username: {uname}",
        f"🆔 ID: <code>{uid}</code>   🌐 {'English' if u.get('lang') == 'en' else 'Hinglish'}\n",
        f"📊 Status: <b>{status}</b>",
        f"📢 Channel membership: {member}\n",
        f"💳 Plan: <b>{plan}</b>",
        f"🟢 Shuru: {started}",
        f"⏳ Din baaki: <b>{left}</b>",
        f"📅 Expiry: {fmt_date(u.get('expires_at'))}",
        f"🎁 Trial liya: {'haan' if u.get('trial_used') else 'nahi'} ({TRIAL_DAYS} din wala)",
        f"🗓️ Joined: {fmt_date(u.get('joined_at'))}",
        f"👀 Last seen: {fmt_date(u.get('last_seen'))}",
    ]
    import referral
    ref_line = referral.admin_referral_line(uid)
    if ref_line:
        lines.append(ref_line)
    daily = "∞" if lim.get("key") == "admin" else lim["daily"]
    lines.append(f"\n📤 Posts: aaj {st['today']} (limit {daily}/task) | 7 din {st['week']} | total {st['total']}")
    tasks = list_tasks(uid)
    lines.append(f"\n📋 <b>Tasks ({len(tasks)}/{lim.get('tasks', 0)})</b>")
    if not tasks:
        lines.append("<i>Abhi koi task nahi bana.</i>")
    for t in tasks[:6]:
        c = t["cfg"]
        lines.append(f"{'⏸️' if t['paused'] else '▶️'} <b>{esc(c.get('name') or '#' + str(t['id']))}</b>"
                     f"  (aaj {st['by_task'].get(t['id'], 0)} post)\n"
                     f"     🏷️ Amazon tag: <code>{esc(c.get('tag') or '—')}</code>\n"
                     f"     📥 Draft: {chan(c.get('source_title'), c.get('source_username'), '—')}\n"
                     f"     📢 Destination: {chan(c.get('channel_title'), c.get('channel_username'), '—')}")
    if len(tasks) > 6:
        lines.append(f"… aur {len(tasks) - 6} task — 📋 Tasks dekho")
    pays = recent_payments(3, uid)
    if pays:
        lines.append("\n💰 <b>Payments</b>")
        for _, prov, amount, cur, days, paid_at, tier in pays:
            amt = f"₹{amount // 100}" if cur == "INR" else f"{amount}⭐"
            lines.append(f"• {amt} {tier_label(tier or '')} ({prov}) — {fmt_date(paid_at)}")

    b = f"{uid}:{seg}:{page}"
    rows = [
        [btn("📋 Tasks dekho", BLUE, callback_data=f"adm:tk:{b}")],
        [btn("🎁 Grant Days", GREEN, callback_data=f"adm:ga:{b}"),
         btn("➖ Reduce Days", RED, callback_data=f"adm:rd:{b}")],
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
        # Forwarder jaisa — seedha numbered list; ID se dhoondhne ka button list mein hai
        text, kb = users_page("all", 0)
        m = await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                            disable_web_page_preview=True)
        track(context, m)
        return
    u = find_user(args[0])
    text, kb = (await user_card(context.bot, u["user_id"])) if u else ("❌ User nahi mila.", None)
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
BC_AUDIENCES = [
    ("all", "👥 Sabko"), ("paid_or_trial", "✅ Plan/Trial chalu"), ("active", "💎 Sirf Paid"),
    ("trial", "🎁 Sirf Trial"), ("expired", "⌛ Plan khatam"), ("en", "🇬🇧 English"), ("hi", "🇮🇳 Hinglish"),
]
BC_NAMES = dict(BC_AUDIENCES, sel="👥 Chune hue users")


def _recall_kb(bc_id):
    return InlineKeyboardMarkup([[btn("↩️ Recall (wapas lo)", RED, callback_data=f"adm:bre:{bc_id}")],
                                 [btn("👑 Panel", callback_data="adm:home")]])


def bc_audience_kb() -> InlineKeyboardMarkup:
    rows, pair = [], []
    for seg, name in BC_AUDIENCES:
        pair.append(btn(f"{name} ({len(list_user_ids(seg))})", callback_data=f"adm:bcgo:{seg}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("👥 Users chunein", BLUE, callback_data="adm:bsel:0")])
    rows.append([btn("❌ Cancel", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def bc_select_page(context, page: int):
    """Broadcast ke liye users chunna — number dabao to ✔️ / hatao."""
    sel = set(context.user_data.get("bc_sel") or [])
    users, total = list_users_page("all", page * PAGE, PAGE)
    pages = max(1, (total + PAGE - 1) // PAGE)
    lines = [f"👥 <b>Broadcast — users chunein</b>   page {page + 1}/{pages}\n"]
    for i, u in enumerate(users, 1):
        mark = "✅ " if u["user_id"] in sel else ""
        lines.append(f"<b>{i}.</b> {mark}{_who(u)} — {_plan_short(u)}")
    lines.append(f"\n✔️ Chune hue: <b>{len(sel)}</b> (page badalne pe bhi yaad rehte hain)")
    nums = [btn(("✅" if u["user_id"] in sel else "") + str(i),
                callback_data=f"adm:bst:{u['user_id']}:{page}") for i, u in enumerate(users, 1)]
    rows = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Pichla", callback_data=f"adm:bsel:{page - 1}"))
    if page + 1 < pages:
        nav.append(btn("Agla ➡️", callback_data=f"adm:bsel:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn(f"✅ Bhejo ({len(sel)})", GREEN, callback_data="adm:bsd"),
                 btn("🗑️ Saaf karo", callback_data=f"adm:bsc:{page}")])
    rows.append([btn("⬅️ Wapas", callback_data="adm:bcback")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def bc_list_screen():
    rows_ = broadcasts.recent(10)
    if not rows_:
        return ("↩️ <b>Recall</b>\n\nPichhle 48 ghante mein koi broadcast nahi (ya sab recall ho chuke).",
                InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
    lines = ["↩️ <b>Pichhle 48 ghante ke broadcast</b>\n"]
    kb = []
    for bc_id, aud, total, sent, created in rows_:
        lines.append(f"#{bc_id} — {BC_NAMES.get(aud, aud)} — ✅ {sent}/{total} — {fmt_date(created)}")
        kb.append([btn(f"↩️ #{bc_id} wapas lo", callback_data=f"adm:bre:{bc_id}")])
    kb.append([btn("⬅️ Panel", callback_data="adm:home")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _start_broadcast(query, context, uid: int, audience: str, ids: list):
    from task_ui import show
    src = context.user_data.pop("bc_src", None)
    if not src:
        await show(query, context, "⚠️ Message nahi mila, /broadcast dobara karein.",
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return
    if broadcasts.is_running():
        context.user_data["bc_src"] = src
        await show(query, context, "⚠️ Ek broadcast pehle se chal raha hai. Thodi der baad try karein.",
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return
    if not ids:
        context.user_data["bc_src"] = src
        await show(query, context, "⚠️ Is group mein koi user nahi.", bc_audience_kb())
        return
    context.user_data.pop("bc_sel", None)
    await show(query, context, f"📣 <b>Broadcast shuru</b> — {BC_NAMES.get(audience, audience)}\n👥 {len(ids)} users")
    context.application.create_task(broadcasts.run(
        context.bot, uid, src[0], src[1], ids, audience, status_msg=query.message,
        mark_bot_blocked=mark_bot_blocked, recall_kb=_recall_kb))


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
        await show(query, context, "📣 <b>Broadcast</b>\n\nJo message bhejna hai wo bhejein (text / photo / video — "
                                   "kuch bhi). Phir main poochunga kisko bhejna hai.",
                   InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
        return True
    if act == "bcback":
        if not context.user_data.get("bc_src"):
            await show(query, context, "⚠️ Message nahi mila, /broadcast dobara karein.",
                       InlineKeyboardMarkup([[btn("⬅️ Panel", callback_data="adm:home")]]))
            return True
        await show(query, context, "📣 <b>Ye message kisko bhejna hai?</b>", bc_audience_kb())
        return True
    if act == "bcgo":
        seg = p[2] if len(p) > 2 else "all"
        if seg not in dict(BC_AUDIENCES):
            return True
        await _start_broadcast(query, context, uid, seg, list_user_ids(seg))
        return True
    if act == "bsel":
        page = int(p[2]) if len(p) > 2 and p[2].isdigit() else 0
        text, kb = bc_select_page(context, page)
        await show(query, context, text, kb)
        return True
    if act == "bst" and len(p) >= 4:
        try:
            target, page = int(p[2]), int(p[3])
        except ValueError:
            return True
        sel = list(context.user_data.get("bc_sel") or [])
        if target in sel:
            sel.remove(target)
        else:
            sel.append(target)
        context.user_data["bc_sel"] = sel
        text, kb = bc_select_page(context, page)
        await show(query, context, text, kb)
        return True
    if act == "bsc":
        context.user_data.pop("bc_sel", None)
        page = int(p[2]) if len(p) > 2 and p[2].isdigit() else 0
        text, kb = bc_select_page(context, page)
        await show(query, context, text, kb)
        return True
    if act == "bsd":
        sel = list(context.user_data.get("bc_sel") or [])
        if not sel:
            await query.answer("Pehle kam se kam ek user chunein.", show_alert=True)
            return True
        await _start_broadcast(query, context, uid, "sel", sel)
        return True
    if act == "bcl":
        text, kb = bc_list_screen()
        await show(query, context, text, kb)
        return True
    if act == "bre" and len(p) > 2 and p[2].isdigit():
        row = broadcasts.get(int(p[2]))
        if not row or row[5]:
            await query.answer("Ye broadcast recall ho chuka ya mila nahi.", show_alert=True)
            return True
        bc_id, aud, total, sent, created, _ = row
        await show(query, context,
                   f"↩️ <b>Broadcast #{bc_id} wapas lein?</b>\n\n👥 {BC_NAMES.get(aud, aud)} — {sent} users ko gaya\n"
                   f"🕐 {fmt_date(created)}\n\nSabke chat se ye message hat jayega (48 ghante ke andar hi ho sakta hai).",
                   InlineKeyboardMarkup([[btn("✅ Haan, wapas lo", RED, callback_data=f"adm:brg:{bc_id}")],
                                         [btn("✖️ Nahi", callback_data="adm:bcl")]]))
        return True
    if act == "brg" and len(p) > 2 and p[2].isdigit():
        bc_id = int(p[2])
        row = broadcasts.get(bc_id)
        if not row or row[5]:
            await query.answer("Ye broadcast recall ho chuka ya mila nahi.", show_alert=True)
            return True
        await show(query, context, f"↩️ Broadcast #{bc_id} wapas le raha hoon...")

        async def go():
            deleted, failed = await broadcasts.recall(context.bot, bc_id)
            try:
                await query.message.edit_text(
                    f"↩️ <b>Broadcast #{bc_id} recall ho gaya</b>\n\n🗑️ Hata diya: {deleted}\n"
                    f"⚠️ Nahi hata (user ne delete kiya / 48 ghante se purana): {failed}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[btn("👑 Panel", callback_data="adm:home")]]))
            except Exception:
                pass
        context.application.create_task(go())
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
        # Grant / Reduce — jaldi wale number + apna number likhne ka option
        grant = act == "ga"
        sign = 1 if grant else -1
        u = get_user(target) or {}
        ask = (f"🎁 <b>Grant Days</b> — {_who(u)}\n📅 Abhi expiry: {fmt_date(u.get('expires_at'))}\n\n"
               "Kitne din <b>jodne</b> hain? Neeche chunein ya apna number likhein.\n"
               "🔔 <i>User ko message jayega.</i>" if grant else
               f"➖ <b>Reduce Days</b> — {_who(u)}\n📅 Abhi expiry: {fmt_date(u.get('expires_at'))}\n\n"
               "Kitne din <b>kaatne</b> hain? Neeche chunein ya apna number likhein.\n"
               "🔕 <i>User ko koi message nahi jayega (silent).</i>")
        quick = [btn(f"{'+' if grant else '−'}{n}", GREEN if grant else RED,
                     callback_data=f"adm:d:{target}:{sign * n}:{seg}:{page}") for n in (1, 3, 7, 15, 30, 90)]
        await show(query, context, ask, InlineKeyboardMarkup([
            quick[:3], quick[3:],
            [btn("✏️ Apna number likhein", callback_data=f"adm:{'gc' if grant else 'rc'}:{target}:{seg}:{page}")],
            [btn("⬅️ Wapas", callback_data=f"adm:v:{target}:{seg}:{page}")],
        ]))
        return True
    if act in ("gc", "rc"):
        grant = act == "gc"
        context.user_data.update(action="adm_days", adm_target=target, adm_sign=1 if grant else -1,
                                 adm_back=f"{seg}:{page}")
        ask = ("🎁 Kitne din <b>jodne</b> hain? Number bhejein (jaise <code>45</code>).\n"
               "🔔 <i>User ko message jayega.</i>" if grant else
               "➖ Kitne din <b>kaatne</b> hain? Number bhejein (jaise <code>5</code>).\n"
               "🔕 <i>User ko koi message nahi jayega.</i>")
        await show(query, context, ask,
                   InlineKeyboardMarkup([[btn("⬅️ Wapas", callback_data=f"adm:{'ga' if grant else 'rd'}:{target}:{seg}:{page}")]]))
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

    text, kb = await user_card(context.bot, target, seg, page)
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
        out, kb = (await user_card(context.bot, u["user_id"])) if u else \
            ("❌ User nahi mila. (User ne /start kiya hona chahiye.)", None)
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
        out, kb = await user_card(context.bot, target, seg or "all", int(page) if page.isdigit() else 0)
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
        context.user_data.pop("bc_sel", None)
        m = await msg.reply_text("📣 <b>Ye message kisko bhejna hai?</b>\n<i>Bracket mein kitne users hain.</i>",
                                 parse_mode=ParseMode.HTML, reply_markup=bc_audience_kb())
        track(context, m)
        return True
    return False

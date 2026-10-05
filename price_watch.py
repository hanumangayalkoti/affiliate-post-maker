"""
price_watch.py — Price drop alert. User product track karta hai (/track),
bot har thodi der mein price check karta hai. Price gira to user ko message,
aur setting ON ho to seedha channel pe post bhi.
"""
import os
import asyncio
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from amazon_api import resolve_amazon_url, extract_asin, is_amazon_url, get_products_by_asins, inr
from database import (
    watch_add, watch_count, watch_list, watch_get, watch_delete,
    watch_all_for_active, watch_update_price, watch_expire,
)
from engine import post_amazon_product, dm_user, extract_urls, setup_problems
from storage import load_config
from users import ADMIN_IDS, is_admin, is_active

logger = logging.getLogger(__name__)
esc = html_lib.escape

MAX_WATCHES         = int(os.getenv("MAX_WATCHES", "25"))
PRICE_CHECK_MINUTES = int(os.getenv("PRICE_CHECK_MINUTES", "60"))
MIN_DROP_PCT        = float(os.getenv("MIN_DROP_PCT", "5"))
WATCH_DAYS          = int(os.getenv("WATCH_DAYS", "30"))

_check_lock = asyncio.Lock()


def _rs(v) -> str:
    return inr(v) or "—"


def _short(t: str, n: int = 45) -> str:
    t = (t or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _parse_price(s: str):
    s = (s or "").replace("₹", "").replace(",", "").strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


# =============================================================================
# ADD / LIST
# =============================================================================
async def add_watch_from_text(uid: int, text: str):
    """Link (+ optional target price) se watch banao. Returns reply text."""
    urls = [u for u in extract_urls(text) if is_amazon_url(u)]
    if not urls:
        return ("⚠️ Amazon product ka link bhejo.\n"
                "Jaise: <code>/track https://amzn.to/xxxx 799</code>\n"
                "<i>(aakhir ka number = target price, optional)</i>")
    if watch_count(uid) >= MAX_WATCHES and not is_admin(uid):
        return f"⚠️ Max {MAX_WATCHES} products track ho sakte hain. /alerts se kuch hatao."

    rest = text.replace(urls[0], " ").split()
    target = None
    for tok in rest:
        if tok.startswith("/"):
            continue
        p = _parse_price(tok)
        if p:
            target = p
            break

    resolved = await resolve_amazon_url(urls[0])
    asin = extract_asin(resolved) or extract_asin(urls[0])
    if not asin:
        return "⚠️ Is link mein product nahi mila (search/deals page track nahi hota)."

    prod = (await get_products_by_asins([asin])).get(asin)
    if not prod or not prod.get("deal_amount"):
        return "⚠️ Amazon se is product ka price nahi mila. Thodi der baad try karo."

    price = float(prod["deal_amount"])
    res = watch_add(uid, asin, prod.get("title", ""), price, target)
    if res == "error":
        return "❌ Save nahi hua, dobara try karo."
    lines = [
        f"✅ <b>{'Tracking update' if res == 'updated' else 'Tracking shuru'}!</b>\n",
        f"🛍️ {esc(_short(prod.get('title', asin), 70))}",
        f"💰 Abhi ka price: <b>{_rs(price)}</b>",
    ]
    if target:
        lines.append(f"🎯 Target: <b>{_rs(target)}</b> — isse neeche aate hi bataunga")
    else:
        lines.append(f"📉 Price {MIN_DROP_PCT:.0f}% ya zyada gira to bataunga")
    lines.append(f"\n<i>Har {PRICE_CHECK_MINUTES} minute mein check hota hai. "
                 f"{WATCH_DAYS} din baad tracking apne aap band.</i>")
    return "\n".join(lines)


def alerts_text(uid: int) -> str:
    rows = watch_list(uid)
    cfg = load_config(uid)
    head = (f"📉 <b>Price Drop Alerts</b> ({len(rows)}/{MAX_WATCHES})\n"
            f"Auto-post: <b>{'✅ ON' if cfg.get('pricedrop_autopost') else '❌ OFF'}</b>\n")
    if not rows:
        return (head + "\nAbhi koi product track nahi ho raha.\n\n"
                "<b>Kaise karein:</b>\n<code>/track amazon-link</code>\n"
                "<code>/track amazon-link 799</code>  ← target price ke saath")
    lines = [head]
    for i, r in enumerate(rows, 1):
        tgt = f" | 🎯 {_rs(r['target_price'])}" if r.get("target_price") else ""
        lines.append(f"{i}. {esc(_short(r.get('title') or r['asin']))}\n"
                     f"   Shuru: {_rs(r['base_price'])} → Abhi: <b>{_rs(r['last_price'])}</b>{tgt}")
    return "\n".join(lines)


def alerts_kb(uid: int) -> InlineKeyboardMarkup:
    rows = []
    for i, r in enumerate(watch_list(uid)[:MAX_WATCHES], 1):
        rows.append([InlineKeyboardButton(f"🗑️ {i}. {_short(r.get('title') or r['asin'], 28)}",
                                          callback_data=f"pw_del_{r['id']}")])
    rows.append([InlineKeyboardButton("➕ Product jodo", callback_data="pw_add")])
    rows.append([InlineKeyboardButton("⚙️ Auto-post setting", callback_data="set_pdauto")])
    return InlineKeyboardMarkup(rows)


async def cmd_track(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    text = update.message.text or ""
    if not extract_urls(text):
        context.user_data["action"] = "wait_track"
        await update.message.reply_text(
            "📉 Jis product ka price track karna hai uska <b>Amazon link</b> bhejo.\n"
            "<i>Chaho to link ke baad target price bhi likh do, jaise:</i>\n"
            "<code>https://amzn.to/xxxx 799</code>", parse_mode=ParseMode.HTML)
        return
    wait = await update.message.reply_text("⏳ Product check ho raha hai...")
    out = await add_watch_from_text(uid, text)
    try:
        await wait.edit_text(out, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text(out, parse_mode=ParseMode.HTML)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    await update.message.reply_text(alerts_text(uid), parse_mode=ParseMode.HTML,
                                    reply_markup=alerts_kb(uid), disable_web_page_preview=True)


async def handle_track_input(update: Update, context, uid: int) -> bool:
    context.user_data.pop("action", None)
    wait = await update.message.reply_text("⏳ Product check ho raha hai...")
    out = await add_watch_from_text(uid, update.message.text or "")
    try:
        await wait.edit_text(out, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text(out, parse_mode=ParseMode.HTML)
    return True


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_watch_callback(query, context, uid: int, data: str) -> bool:
    if data == "pw_add":
        context.user_data["action"] = "wait_track"
        await query.message.reply_text("📉 Product ka Amazon link bhejo (target price optional):")
        return True

    if data.startswith("pw_del_"):
        try:
            wid = int(data.split("_")[2])
        except (IndexError, ValueError):
            return True
        watch_delete(uid, wid)
        try:
            await query.edit_message_text(alerts_text(uid), parse_mode=ParseMode.HTML,
                                          reply_markup=alerts_kb(uid), disable_web_page_preview=True)
        except Exception:
            pass
        return True

    if data.startswith("pw_post_"):
        try:
            wid = int(data.split("_")[2])
        except (IndexError, ValueError):
            return True
        w = watch_get(wid)
        if not w or w["user_id"] != uid:
            await query.message.reply_text("⚠️ Ye tracking ab nahi hai.")
            return True
        if not is_active(uid):
            await query.message.reply_text("⏸️ Plan khatam hai — /plan se renew karo.")
            return True
        cfg = load_config(uid)
        probs = setup_problems(cfg)
        if probs:
            await query.message.reply_text("⚠️ " + "\n".join(probs))
            return True
        prod = (await get_products_by_asins([w["asin"]], max_age_minutes=15)).get(w["asin"])
        if not prod:
            await query.message.reply_text("⚠️ Amazon se data nahi mila, thodi der baad try karo.")
            return True
        prod = dict(prod)
        prod["drop_note"] = f"Price Drop: {_rs(w['base_price'])} → {_rs(prod.get('deal_amount'))}"
        status, detail, _ = await post_amazon_product(context, uid, prod, cfg, force=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("✅ Channel pe post ho gaya!" if status == "posted"
                                       else f"❌ Post nahi hua: {detail}")
        return True
    return False


# =============================================================================
# CHECKER JOB
# =============================================================================
async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    if _check_lock.locked():
        return
    async with _check_lock:
        try:
            await _run_check(context)
        except Exception as e:
            logger.exception(f"Price check fail: {e}")


async def _run_check(context):
    bot = context.bot

    for uid, title in watch_expire(WATCH_DAYS):
        await dm_user(bot, uid, f"⌛ {WATCH_DAYS} din poore — tracking band: "
                                f"{esc(_short(title or 'product'))}\n/track se dobara shuru kar sakte ho.",
                      parse_mode=ParseMode.HTML)

    watches = watch_all_for_active(ADMIN_IDS)
    if not watches:
        return
    asins = list({w["asin"] for w in watches})
    fresh = await get_products_by_asins(asins, max_age_minutes=max(5, PRICE_CHECK_MINUTES - 5))
    logger.info(f"Price check: {len(watches)} watches, {len(asins)} ASIN, {len(fresh)} mile")

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.bot = bot

    cfg_cache = {}
    for w in watches:
        p = fresh.get(w["asin"])
        if not p or not p.get("deal_amount"):
            continue
        now_p  = float(p["deal_amount"])
        last_p = float(w["last_price"])
        target = float(w["target_price"]) if w.get("target_price") else None

        # last_price = "reference" price. Price badha to reference upar; thoda-thoda
        # gira to reference wahi rehta hai — taaki dheere-dheere gira 10% bhi pakda jaye.
        if now_p >= last_p - 0.5:
            if now_p > last_p + 0.5:
                watch_update_price(w["id"], now_p, alerted=False)
            continue

        dropped_pct = (last_p - now_p) / last_p * 100 if last_p else 0
        hit_target  = target is not None and now_p <= target < last_p
        if not (dropped_pct >= MIN_DROP_PCT or hit_target):
            continue
        watch_update_price(w["id"], now_p, alerted=True)

        uid = w["user_id"]
        cfg = cfg_cache.get(uid) or load_config(uid)
        cfg_cache[uid] = cfg
        title = _short(p.get("title") or w.get("title") or w["asin"], 70)

        posted_note = ""
        auto_ok = cfg.get("pricedrop_autopost") and not setup_problems(cfg)
        if auto_ok:
            prod = dict(p)
            prod["drop_note"] = f"Price Drop: {_rs(last_p)} → {_rs(now_p)}"
            status, detail, _ = await post_amazon_product(ctx, uid, prod, cfg, force=True)
            posted_note = ("\n\n✅ Channel pe apne aap post kar diya." if status == "posted"
                           else f"\n\n❌ Auto-post nahi hua: {esc(detail)}")
            await asyncio.sleep(1)

        kb = None
        if not auto_ok:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Channel pe post karo", callback_data=f"pw_post_{w['id']}")],
                [InlineKeyboardButton("🗑️ Tracking band karo", callback_data=f"pw_del_{w['id']}")],
            ])
        why = f"🎯 Target {_rs(target)} aa gaya!" if hit_target else f"📉 {dropped_pct:.0f}% gira"
        await dm_user(bot, uid,
                      f"🔔 <b>Price gira!</b>\n\n🛍️ {esc(title)}\n"
                      f"💰 {_rs(last_p)} → <b>{_rs(now_p)}</b>  ({why})"
                      + posted_note,
                      parse_mode=ParseMode.HTML, reply_markup=kb)
        await asyncio.sleep(0.1)

"""
billing.py — plans (Basic / Pro / Premium), Razorpay (UPI/Card, automatic),
Telegram Stars, aur Razorpay webhook ke liye chhota web server.

Upgrade / downgrade: bache din ki keemat naye plan ke din mein jud jaati hai.
Expiry hamesha raat 12 baje (IST).
"""
import os
import time
import hmac
import json
import base64
import hashlib
import logging
import html as html_lib

import aiohttp
from aiohttp import web
from telegram import InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from alerts import notify_admins, who
from engine import fmt_date, dm_user
from tiers import TIERS, TIER_ORDER, PLAN_DAYS, new_expiry, tier_label
from ui import btn, tr, GREEN, BLUE
from users import (
    get_user, get_lang, is_admin, is_active, days_left, limits, upsert_user,
    payment_exists, payment_status_by_receipt, payment_create_pending,
    payment_mark_paid_razorpay, payment_record_stars,
)
from storage import utcnow

logger = logging.getLogger(__name__)
esc = html_lib.escape

RAZORPAY_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_WEBHOOK_PATH   = "/" + os.getenv("RAZORPAY_WEBHOOK_PATH", "/webhooks/razorpay").strip().lstrip("/")
STARS_ENABLED           = os.getenv("STARS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SUPPORT_USERNAME        = os.getenv("SUPPORT_USERNAME", "").strip()
BOT_NAME                = os.getenv("BOT_NAME", "Deal Post Maker")


def razorpay_ready() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET)


def support_line(lang: str = "hi") -> str:
    if SUPPORT_USERNAME:
        u = esc("@" + SUPPORT_USERNAME.lstrip("@"))
        return tr(lang, f"Need help? Message {u}", f"Madad chahiye? {u} ko message karein")
    return ""


# =============================================================================
# PLAN SCREEN
# =============================================================================
def _status_lines(uid: int, lang: str) -> list:
    u = get_user(uid) or {}
    lim = limits(uid, u)
    if is_active(uid, u):
        left = days_left(u)
        kind = tr(lang, " (free trial)", " (free trial)") if u.get("is_trial") else ""
        return [tr(lang, f"✅ <b>Current plan:</b> {lim['emoji']} {lim['name']}{kind}",
                   f"✅ <b>Aapka plan:</b> {lim['emoji']} {lim['name']}{kind}"),
                tr(lang, f"⏳ {left:.0f} days left — ends {fmt_date(u.get('expires_at'))}",
                   f"⏳ {left:.0f} din baaki — khatam: {fmt_date(u.get('expires_at'))}")]
    if u.get("expires_at"):
        return [tr(lang, f"❌ <b>Your plan ended</b> ({fmt_date(u.get('expires_at'))})",
                   f"❌ <b>Aapka plan khatam ho gaya</b> ({fmt_date(u.get('expires_at'))})")]
    return [tr(lang, "❌ <b>No plan yet</b>", "❌ <b>Abhi koi plan nahi hai</b>")]


def plan_text(uid: int) -> str:
    lang = get_lang(uid)
    if is_admin(uid):
        missing = [n for n, v in (("RAZORPAY_KEY_ID", RAZORPAY_KEY_ID),
                                  ("RAZORPAY_KEY_SECRET", RAZORPAY_KEY_SECRET),
                                  ("RAZORPAY_WEBHOOK_SECRET", RAZORPAY_WEBHOOK_SECRET)) if not v]
        rz = "✅ ready" if not missing else "❌ band — missing: " + ", ".join(missing)
        lines = ["👑 <b>Aap admin hain</b> — aapke liye sab hamesha chalu hai.\n",
                 "<b>Payment status (sirf aapko dikhta hai):</b>",
                 f"💳 Razorpay: {rz}",
                 f"⭐ Stars: {'✅ ON' if STARS_ENABLED else '❌ OFF'}",
                 f"🔗 Webhook: <code>{esc(RAZORPAY_WEBHOOK_PATH)}</code> ya sirf domain\n",
                 "<b>Plans (users ko aise dikhte hain):</b>"]
    else:
        lines = ["💎 <b>Plans</b>\n"] + _status_lines(uid, lang) + [""]
    for k in TIER_ORDER:
        t = TIERS[k]
        card = tr(lang, "✅ Image Card", "✅ Image Card") if t["card"] else tr(lang, "❌ Image Card", "❌ Image Card")
        lines.append(f"{t['emoji']} <b>{t['name']}</b> — ₹{t['inr']} / {PLAN_DAYS} {tr(lang, 'days', 'din')}")
        lines.append(tr(lang, f"     📋 {t['tasks']} task  •  📤 {t['daily']} posts/day per task  •  {card}",
                        f"     📋 {t['tasks']} task  •  📤 {t['daily']} post/din har task  •  {card}"))
    lines.append(tr(lang,
                    "\n<i>All plans include every other feature. Changing plan? The value of your "
                    "remaining days is added to the new plan — nothing is lost.</i>",
                    "\n<i>Baaki saare features har plan mein hain. Plan badalne pe bache din ki keemat "
                    "naye plan mein jud jaati hai — kuch nahi kat-ta.</i>"))
    s = support_line(lang)
    if s and not is_admin(uid):
        lines += ["", s]
    return "\n".join(lines)


def plan_kb(uid: int):
    if is_admin(uid):
        return InlineKeyboardMarkup([[btn("🏠 Home", callback_data="home")]])
    rows = [[btn(f"{TIERS[k]['emoji']} {TIERS[k]['name']} — ₹{TIERS[k]['inr']}  •  Buy Now",
                 BLUE if k == "pro" else "", callback_data=f"plan:{k}")] for k in TIER_ORDER]
    rows.append([btn("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def tier_text(uid: int, tier: str) -> str:
    lang = get_lang(uid)
    t = TIERS[tier]
    u = get_user(uid) or {}
    exp = new_expiry(utcnow(), u.get("expires_at"), u.get("tier"), bool(u.get("is_trial")), tier)
    lines = [f"{t['emoji']} <b>{t['name']}</b> — ₹{t['inr']} / {PLAN_DAYS} {tr(lang, 'days', 'din')}\n",
             tr(lang, f"📋 {t['tasks']} task(s)\n📤 {t['daily']} posts per day per task\n",
                f"📋 {t['tasks']} task\n📤 {t['daily']} post roz har task\n")
             + ("🎨 Image Card ✅" if t["card"] else "🎨 Image Card ❌"),
             tr(lang, f"\n📅 After buying, your plan runs till <b>{fmt_date(exp)}</b>",
                f"\n📅 Khareedne ke baad plan <b>{fmt_date(exp)}</b> tak chalega")]
    cur = limits(uid, u)
    if is_active(uid, u) and not u.get("is_trial") and cur.get("key") not in (None, tier):
        lines.append(tr(lang, "<i>(includes the value of your remaining days)</i>",
                        "<i>(bache din ki keemat jod ke)</i>"))
        if TIERS[tier]["tasks"] < cur["tasks"]:
            lines.append(tr(lang, f"⚠️ This plan allows {TIERS[tier]['tasks']} running task(s) — extra tasks "
                                  "will be paused (not deleted).",
                            f"⚠️ Is plan mein {TIERS[tier]['tasks']} task chalenge — baaki tasks pause ho "
                            "jayenge (delete nahi)."))
    lines.append(tr(lang, "\nChoose how to pay 👇", "\nPayment ka tareeka chunein 👇"))
    return "\n".join(lines)


def tier_kb(uid: int, tier: str):
    lang = get_lang(uid)
    t = TIERS[tier]
    rows = []
    if razorpay_ready():
        rows.append([btn(f"💳 UPI / Card — ₹{t['inr']}  •  Buy Now", GREEN, callback_data=f"pay:rzp:{tier}")])
    if STARS_ENABLED:
        rows.append([btn(f"⭐ Telegram Stars — {t['stars']}⭐", BLUE, callback_data=f"pay:st:{tier}")])
    rows.append([btn(tr(lang, "⬅️ All Plans", "⬅️ Saare Plans"), callback_data="open_plan")])
    return InlineKeyboardMarkup(rows)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    from ui import track
    m = await update.message.reply_text(plan_text(uid), parse_mode=ParseMode.HTML,
                                        reply_markup=plan_kb(uid), disable_web_page_preview=True)
    track(context, m)


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Telegram Stars ke rules ke hisaab se zaroori command."""
    from ui import track
    lang = get_lang(uid)
    m = await update.message.reply_text(
        tr(lang,
           "🧾 <b>Payment Support</b>\n\nPaid but the plan didn't start? Or any other problem?\n"
           "Send your Telegram ID and a payment screenshot — we'll fix it quickly.\n\n"
           f"🆔 Your ID: <code>{uid}</code>\n",
           "🧾 <b>Payment Support</b>\n\nPayment hua par plan chalu nahi hua? Ya koi aur dikkat?\n"
           "Apna Telegram ID aur payment ka screenshot bhejein — jaldi theek kar denge.\n\n"
           f"🆔 Aapka ID: <code>{uid}</code>\n")
        + (support_line(lang) or tr(lang, "Just message here, the admin will see it.",
                                    "Yahin message kar dein, admin dekh lenge.")),
        parse_mode=ParseMode.HTML)
    track(context, m)


# =============================================================================
# RAZORPAY
# =============================================================================
async def _razorpay_create_link(uid: int, tier: str, receipt: str) -> str:
    t = TIERS[tier]
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    payload = {
        "amount": t["inr"] * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": receipt,
        "description": f"{BOT_NAME} — {t['name']} ({PLAN_DAYS} days)",
        "customer": {"name": f"User {uid}"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"user_id": str(uid), "receipt": receipt, "days": str(PLAN_DAYS), "tier": tier},
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
        async with s.post("https://api.razorpay.com/v1/payment_links", json=payload,
                          headers={"Authorization": f"Basic {auth}"}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {}
            if resp.status >= 400 or not data.get("id") or not data.get("short_url"):
                err = (data.get("error") or {}).get("description") or f"HTTP {resp.status}"
                raise RuntimeError(err)
            # Pehle DB mein save — fail hua to link mat do, warna paisa kat ke plan nahi milega
            if not payment_create_pending(uid, "razorpay", data["id"], receipt,
                                          t["inr"] * 100, "INR", PLAN_DAYS, tier):
                raise RuntimeError("payment record save nahi hua")
            return data["short_url"]


def _verify_signature(raw: bytes, signature: str) -> bool:
    if not signature or not raw or not RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_paid(payload: dict):
    """(receipt, link_id, payment_id) ya None."""
    event = payload.get("event")
    body = payload.get("payload") or {}
    pay = ((body.get("payment") or {}).get("entity")) or {}
    if event == "payment_link.paid":
        link = ((body.get("payment_link") or {}).get("entity")) or {}
        notes = pay.get("notes") or link.get("notes") or {}
        return (str(notes.get("receipt") or link.get("reference_id") or ""),
                str(link.get("id") or ""), str(pay.get("id") or "unknown"))
    if event == "payment.captured":
        notes = pay.get("notes") or {}
        receipt = str(notes.get("receipt") or "")
        if receipt:
            return receipt, "", str(pay.get("id") or "unknown")
    return None


async def after_paid(application, uid: int, tier: str, new_exp, how: str, amount_text: str, old_tier,
                     amount_paise: int = 0, payment_ref: str = ""):
    """Payment ke baad: user ko report, tasks ki limit, admin ko khabar, referrer ko commission."""
    from task_ui import enforce_task_limit
    lang = get_lang(uid)
    paused = enforce_task_limit(uid)
    t = TIERS[tier]
    text = tr(lang,
              f"🎉 <b>Payment received — thank you!</b>\n\n✅ Plan: <b>{t['emoji']} {t['name']}</b>\n"
              f"📅 Valid till: <b>{fmt_date(new_exp)}</b>\n📋 {t['tasks']} task(s) • 📤 {t['daily']} posts/day per task",
              f"🎉 <b>Payment mil gaya — shukriya!</b>\n\n✅ Plan: <b>{t['emoji']} {t['name']}</b>\n"
              f"📅 Chalega: <b>{fmt_date(new_exp)}</b> tak\n📋 {t['tasks']} task • 📤 {t['daily']} post/din har task")
    if paused:
        text += tr(lang, f"\n\n⏸️ {len(paused)} extra task(s) were paused (your plan allows {t['tasks']}). "
                         "Choose which to run in /tasks.",
                   f"\n\n⏸️ {len(paused)} extra task pause kiye (plan mein {t['tasks']} chal sakte hain). "
                   "Kaunsa chalana hai /tasks mein chunein.")
    await dm_user(application.bot, uid, text, parse_mode=ParseMode.HTML)
    change = ""
    if old_tier and old_tier != tier and old_tier in TIERS:
        up = TIER_ORDER.index(tier) > TIER_ORDER.index(old_tier)
        change = f"\n{'⬆️ Upgrade' if up else '⬇️ Downgrade'}: {tier_label(old_tier)} → {tier_label(tier)}"
    await notify_admins(application.bot,
                        f"💰 <b>Naya payment</b> ({how})\n👤 {who(uid)}\n"
                        f"💵 {esc(amount_text)} — {tier_label(tier)}{change}\n📅 Expiry: {fmt_date(new_exp)}")
    # Referral commission — har payment pe (renewal bhi), ek payment pe ek hi baar
    try:
        import referral
        await referral.on_payment(application.bot, uid, amount_paise or TIERS[tier]["inr"] * 100,
                                  tier, how, payment_ref)
    except Exception as e:
        logger.error(f"Referral credit fail ({uid}): {e}")


def build_web_app(application) -> web.Application:
    app = web.Application()

    async def health(_request):
        return web.json_response({"status": "ok"})

    async def razorpay_webhook(request):
        raw = await request.read()
        if not _verify_signature(raw, request.headers.get("X-Razorpay-Signature", "")):
            logger.warning("Razorpay webhook — galat signature")
            return web.json_response({"error": "invalid signature"}, status=401)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        parsed = _parse_paid(payload)
        logger.info(f"Razorpay webhook: {payload.get('event')} → {parsed}")
        if not parsed:
            return web.json_response({"status": "ignored"})
        receipt, link_id, payment_id = parsed
        res = payment_mark_paid_razorpay(receipt, link_id, payment_id)
        if res is None:
            st = payment_status_by_receipt(receipt)
            if st in ("pending", "error"):
                logger.error(f"Razorpay activation fail, retry hoga: {receipt}")
                return web.json_response({"error": "retry"}, status=500)
            return web.json_response({"status": "ignored"})
        uid, tier, days, new_exp, amount, old_tier = res
        try:
            await after_paid(application, uid, tier, new_exp, "Razorpay", f"₹{amount // 100}", old_tier,
                             amount_paise=amount, payment_ref=f"rzp:{receipt or link_id or payment_id}")
        except Exception as e:
            logger.error(f"After-paid notify fail: {e}")
        return web.json_response({"status": "ok"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post(RAZORPAY_WEBHOOK_PATH, razorpay_webhook)
    if RAZORPAY_WEBHOOK_PATH != "/":
        app.router.add_post("/", razorpay_webhook)     # sirf domain daala ho tab bhi chale
    return app


async def start_web_server(application):
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(build_web_app(application))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    application.bot_data["web_runner"] = runner
    logger.info(f"Web server chalu — port {port} (Razorpay webhook: {RAZORPAY_WEBHOOK_PATH} aur /)")


async def stop_web_server(application):
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


# =============================================================================
# TELEGRAM STARS
# =============================================================================
async def send_stars_invoice(bot, uid: int, tier: str):
    t = TIERS[tier]
    await bot.send_invoice(
        chat_id=uid,
        title=f"{BOT_NAME} — {t['name']}",
        description=f"{t['name']} plan — {PLAN_DAYS} days. {t['tasks']} task(s), {t['daily']} posts/day per task.",
        payload=f"plan:{tier}:{PLAN_DAYS}:{uid}:{int(time.time())}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(f"{t['name']} — {PLAN_DAYS} days", t["stars"])],
    )


def _parse_stars_payload(payload: str):
    parts = (payload or "").split(":")
    if len(parts) == 5 and parts[0] == "plan" and parts[1] in TIERS and parts[2].isdigit():
        return parts[1], int(parts[2]), parts[3]
    return None


async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    p = _parse_stars_payload(q.invoice_payload)
    ok = (p is not None and q.currency == "XTR" and p[2] == str(q.from_user.id)
          and q.total_amount == TIERS[p[0]]["stars"] and p[1] == PLAN_DAYS)
    if ok:
        await q.answer(ok=True)
    else:
        lang = get_lang(q.from_user.id)
        await q.answer(ok=False, error_message=tr(lang, "This invoice is old. Get a new one from /plan.",
                                                  "Ye invoice purana ho gaya. /plan se naya lein."))


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    sp = msg.successful_payment if msg else None
    if not sp or not msg.from_user:
        return
    uid = msg.from_user.id
    lang = get_lang(uid)
    upsert_user(uid, msg.from_user.username or "", msg.from_user.first_name or "")
    p = _parse_stars_payload(sp.invoice_payload)
    tier, days = (p[0], p[1]) if p else ("pro", PLAN_DAYS)
    charge = sp.telegram_payment_charge_id
    res = payment_record_stars(uid, charge, sp.total_amount, days, tier)
    if res is not None:
        new_exp, old_tier = res
        await after_paid(context.application, uid, tier, new_exp, "Telegram Stars",
                         f"{sp.total_amount}⭐", old_tier,
                         amount_paise=TIERS[tier]["inr"] * 100, payment_ref=f"stars:{charge}")
        return
    if payment_exists("stars", charge):
        await msg.reply_text(tr(lang, "✅ This payment is already recorded. Check /plan.",
                                "✅ Ye payment pehle hi record ho chuka hai. /plan dekhein."))
        return
    logger.error(f"Stars payment save FAIL: user {uid}, charge {charge}")
    await msg.reply_text(tr(lang, "⚠️ Payment received, but activating the plan failed. The admin has been "
                                  "told and will fix it soon. /paysupport",
                            "⚠️ Payment mil gaya par plan chalu karne mein dikkat aayi. Admin ko bata diya "
                            "hai, jaldi theek ho jayega. /paysupport"))
    await notify_admins(context.bot,
                        f"🚨 <b>Stars payment save nahi hua!</b>\nUser {who(uid)}, {sp.total_amount}⭐ "
                        f"({tier}), charge <code>{esc(charge)}</code>")


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_billing_callback(query, context, uid: int, data: str) -> bool:
    from task_ui import show
    lang = get_lang(uid)

    if data == "open_plan":
        await show(query, context, plan_text(uid), plan_kb(uid))
        return True

    if data.startswith("plan:"):
        tier = data.split(":", 1)[1]
        if tier in TIERS and not is_admin(uid):
            await show(query, context, tier_text(uid, tier), tier_kb(uid, tier))
        return True

    if data.startswith("pay:"):
        parts = data.split(":")
        if len(parts) != 3 or parts[2] not in TIERS:
            return True
        how, tier = parts[1], parts[2]
        t = TIERS[tier]
        if how == "rzp":
            if not razorpay_ready():
                await query.answer(tr(lang, "UPI/Card payment is off right now.", "UPI/Card payment abhi band hai."),
                                   show_alert=True)
                return True
            receipt = f"dpm{uid}{tier[0]}{int(time.time())}"[:40]
            try:
                url = await _razorpay_create_link(uid, tier, receipt)
            except Exception as e:
                logger.error(f"Razorpay link fail: {e}")
                await notify_admins(context.bot,
                                    f"🚨 <b>Razorpay link nahi bana</b> ({who(uid)})\n<code>{esc(str(e)[:200])}</code>\n"
                                    "<i>RAZORPAY_KEY_ID / SECRET check karo (live/test ek hi mode).</i>")
                await query.answer(tr(lang, "❌ Couldn't create the payment link. Try again in a bit.",
                                      "❌ Payment link nahi bana. Thodi der baad try karein."), show_alert=True)
                return True
            await show(query, context,
                       tr(lang, f"💳 <b>{t['emoji']} {t['name']} — ₹{t['inr']}</b>\n\n"
                                "Tap <b>Buy Now</b> and pay with UPI / Card / Netbanking.\n"
                                "Your plan starts automatically — you'll get a message here.",
                          f"💳 <b>{t['emoji']} {t['name']} — ₹{t['inr']}</b>\n\n"
                          "<b>Buy Now</b> dabayein aur UPI / Card / Netbanking se pay karein.\n"
                          "Payment hote hi plan apne aap chalu — yahin message aayega."),
                       InlineKeyboardMarkup([[btn(f"💳 Buy Now — ₹{t['inr']}", GREEN, url=url)],
                                             [btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"plan:{tier}")]]))
            return True
        if how == "st":
            if not STARS_ENABLED:
                await query.answer(tr(lang, "Stars payment is off right now.", "Stars payment abhi band hai."),
                                   show_alert=True)
                return True
            try:
                await send_stars_invoice(context.bot, uid, tier)
            except Exception as e:
                logger.error(f"Stars invoice fail: {e}")
                await query.answer(tr(lang, "❌ Couldn't create the Stars invoice.", "❌ Stars invoice nahi bana."),
                                   show_alert=True)
            return True
    return False

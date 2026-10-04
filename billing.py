"""
billing.py — plan, Razorpay (UPI/Card, automatic) aur Telegram Stars
(Telegram ke andar, automatic). Razorpay webhook ke liye chhota web server.
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
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from engine import fmt_date, dm_user
from users import (
    get_user, is_admin, is_active, days_left, ADMIN_IDS, upsert_user, payment_exists, payment_status_by_receipt,
    payment_create_pending, payment_mark_paid_razorpay, payment_record_stars,
)

logger = logging.getLogger(__name__)
esc = html_lib.escape

PLAN_DAYS   = int(os.getenv("PLAN_DAYS", "30"))
PRICE_INR   = int(os.getenv("PRICE_INR", "100"))
PRICE_STARS = int(os.getenv("PRICE_STARS", "90"))

RAZORPAY_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_WEBHOOK_PATH   = "/" + os.getenv("RAZORPAY_WEBHOOK_PATH", "/webhooks/razorpay").strip().lstrip("/")
STARS_ENABLED           = os.getenv("STARS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
SUPPORT_USERNAME        = os.getenv("SUPPORT_USERNAME", "").strip()
BOT_NAME                = os.getenv("BOT_NAME", "Deal Post Maker")


def razorpay_ready() -> bool:
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET and RAZORPAY_WEBHOOK_SECRET)


def support_line() -> str:
    if SUPPORT_USERNAME:
        return f"Madad chahiye? {esc('@' + SUPPORT_USERNAME.lstrip('@'))} ko message karo."
    return ""


# =============================================================================
# PLAN SCREEN
# =============================================================================
def plan_text(uid: int) -> str:
    if is_admin(uid):
        return "👑 <b>Tum admin ho</b> — tumhare liye plan ki zaroorat nahi, sab hamesha chalu hai."
    u = get_user(uid) or {}
    if is_active(uid, u):
        left = days_left(u)
        status = (f"✅ <b>Plan chalu hai</b>\n"
                  f"⏳ Bacha: <b>{left:.0f} din</b> (khatam: {fmt_date(u.get('expires_at'))})")
    elif u.get("expires_at"):
        status = f"❌ <b>Plan khatam ho gaya</b> ({fmt_date(u.get('expires_at'))})"
    else:
        status = "❌ <b>Abhi koi plan nahi hai</b>"

    lines = [
        "💳 <b>Plan</b>\n",
        status,
        "",
        f"📦 <b>{PLAN_DAYS} din</b> — ₹{PRICE_INR}"
        + (f"  ya  {PRICE_STARS}⭐" if STARS_ENABLED else ""),
        "",
        "Plan mein sab kuch milta hai: unlimited channel posts, apna affiliate tag, "
        "draft channel, park mode, price drop alerts, stats — sab.",
        "",
        "<i>Plan chalu hone pe din aage jud jaate hain — jaldi renew karne se "
        "kuch nahi kat-ta.</i>",
    ]
    s = support_line()
    if s:
        lines += ["", s]
    return "\n".join(lines)


def plan_kb(uid: int):
    if is_admin(uid):
        return None
    rows = []
    if razorpay_ready():
        rows.append([InlineKeyboardButton(f"💳 UPI / Card se ₹{PRICE_INR}", callback_data="pay_rzp")])
    if STARS_ENABLED:
        rows.append([InlineKeyboardButton(f"⭐ Telegram Stars se {PRICE_STARS}⭐", callback_data="pay_stars")])
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    kb = plan_kb(uid)
    extra = ""
    if kb is None and not is_admin(uid):
        extra = "\n\n⚠️ Abhi online payment band hai. " + (support_line() or "Admin se baat karo.")
    await update.message.reply_text(plan_text(uid) + extra, parse_mode=ParseMode.HTML,
                                    reply_markup=kb, disable_web_page_preview=True)


async def cmd_paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Telegram Stars ke rules ke hisaab se zaroori command."""
    await update.message.reply_text(
        "🧾 <b>Payment Support</b>\n\n"
        "Payment hua par plan chalu nahi hua? Ya koi aur dikkat?\n"
        "Apna Telegram ID aur payment ka screenshot bhejo — jaldi se jaldi theek kar denge.\n\n"
        f"🆔 Tumhara ID: <code>{uid}</code>\n"
        + (support_line() or "Isi bot pe message kar do, admin dekh lega."),
        parse_mode=ParseMode.HTML)


# =============================================================================
# RAZORPAY
# =============================================================================
async def _razorpay_create_link(uid: int, receipt: str) -> str:
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    payload = {
        "amount": PRICE_INR * 100,
        "currency": "INR",
        "accept_partial": False,
        "reference_id": receipt,
        "description": f"{BOT_NAME} — {PLAN_DAYS} din plan",
        "customer": {"name": f"User {uid}"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"user_id": str(uid), "receipt": receipt, "days": str(PLAN_DAYS)},
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
            payment_create_pending(uid, "razorpay", data["id"], receipt,
                                   PRICE_INR * 100, "INR", PLAN_DAYS)
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


async def _after_paid(bot, uid: int, days: int, new_exp, how: str, amount_text: str):
    await dm_user(bot, uid,
                  f"🎉 <b>Payment mil gaya — shukriya!</b>\n\n"
                  f"✅ {days} din jud gaye\n"
                  f"📅 Plan ab chalega: <b>{fmt_date(new_exp)}</b> tak\n\n"
                  f"Ab deals bhejte raho 🚀",
                  parse_mode=ParseMode.HTML)
    u = get_user(uid) or {}
    who = f"@{u.get('username')}" if u.get("username") else (u.get("first_name") or "")
    for aid in ADMIN_IDS:
        await dm_user(bot, aid,
                      f"💰 <b>Naya payment</b> ({how})\n"
                      f"👤 {esc(who)} <code>{uid}</code>\n"
                      f"💵 {esc(amount_text)} — {days} din\n"
                      f"📅 Expiry: {fmt_date(new_exp)}",
                      parse_mode=ParseMode.HTML)


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
        if not parsed:
            return web.json_response({"status": "ignored"})
        receipt, link_id, payment_id = parsed
        res = payment_mark_paid_razorpay(receipt, link_id, payment_id)
        if res is None:
            st = payment_status_by_receipt(receipt)
            if st in ("pending", "error"):
                # Hamara payment hai par activate nahi hua — 500 do, Razorpay dobara bhejega
                logger.error(f"Razorpay activation fail, retry hoga: {receipt}")
                return web.json_response({"error": "retry"}, status=500)
            # Pehle se paid, ya hamara payment hi nahi (jaise Auto Forwarder ka)
            return web.json_response({"status": "ignored"})
        if res:
            uid, days, new_exp, amount = res
            try:
                await _after_paid(application.bot, uid, days, new_exp, "Razorpay",
                                  f"₹{amount // 100}")
            except Exception as e:
                logger.error(f"After-paid notify fail: {e}")
        return web.json_response({"status": "ok"})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post(RAZORPAY_WEBHOOK_PATH, razorpay_webhook)
    return app


async def start_web_server(application):
    port = int(os.getenv("PORT", "8080"))
    runner = web.AppRunner(build_web_app(application))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    application.bot_data["web_runner"] = runner
    logger.info(f"Web server chalu — port {port} (Razorpay webhook: {RAZORPAY_WEBHOOK_PATH})")


async def stop_web_server(application):
    runner = application.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()


# =============================================================================
# TELEGRAM STARS
# =============================================================================
def _stars_payload(uid: int) -> str:
    return f"plan:{PLAN_DAYS}:{uid}:{int(time.time())}"


async def send_stars_invoice(bot, uid: int):
    await bot.send_invoice(
        chat_id=uid,
        title=f"{BOT_NAME} — {PLAN_DAYS} din",
        description=f"{PLAN_DAYS} din ka plan. Payment hote hi plan chalu ho jayega.",
        payload=_stars_payload(uid),
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(f"{PLAN_DAYS} din plan", PRICE_STARS)],
    )


async def handle_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    parts = (q.invoice_payload or "").split(":")
    ok = (len(parts) == 4 and parts[0] == "plan" and q.currency == "XTR"
          and parts[2] == str(q.from_user.id) and q.total_amount == PRICE_STARS
          and parts[1] == str(PLAN_DAYS))
    if ok:
        await q.answer(ok=True)
    else:
        await q.answer(ok=False, error_message="Ye invoice purana ho gaya. /plan se naya lo.")


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    sp = msg.successful_payment if msg else None
    if not sp or not msg.from_user:
        return
    uid = msg.from_user.id
    upsert_user(uid, msg.from_user.username or "", msg.from_user.first_name or "")
    parts = (sp.invoice_payload or "").split(":")
    days = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else PLAN_DAYS
    charge = sp.telegram_payment_charge_id
    new_exp = payment_record_stars(uid, charge, sp.total_amount, days)
    if new_exp is not None:
        await _after_paid(context.bot, uid, days, new_exp, "Telegram Stars", f"{sp.total_amount}⭐")
        return
    if payment_exists("stars", charge):
        await msg.reply_text("✅ Ye payment pehle hi record ho chuka hai. /plan se status dekho.")
        return
    # Paisa aaya par save nahi hua — admin ko turant batao, user ko bharosa do
    logger.error(f"Stars payment save FAIL: user {uid}, charge {charge}")
    await msg.reply_text("⚠️ Payment mil gaya hai par plan chalu karne mein dikkat aayi. "
                         "Admin ko bata diya hai, jaldi theek ho jayega. /paysupport")
    for aid in ADMIN_IDS:
        await dm_user(context.bot, aid,
                      f"🚨 <b>Stars payment save nahi hua!</b>\nUser <code>{uid}</code>, "
                      f"{sp.total_amount}⭐, charge <code>{esc(charge)}</code>\n"
                      f"Manual: <code>/adddays {uid} {days}</code>", parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle_billing_callback(query, context, uid: int, data: str) -> bool:
    if data == "pay_rzp":
        if not razorpay_ready():
            await query.message.reply_text("⚠️ UPI/Card payment abhi band hai.")
            return True
        receipt = f"dpm{uid}t{int(time.time())}"[:40]
        try:
            url = await _razorpay_create_link(uid, receipt)
        except Exception as e:
            logger.error(f"Razorpay link fail: {e}")
            await query.message.reply_text("❌ Payment link nahi ban paya. Thodi der baad try karo.\n"
                                           + support_line())
            return True
        await query.message.reply_text(
            f"💳 <b>₹{PRICE_INR} — {PLAN_DAYS} din</b>\n\n"
            f"Neeche button dabao aur UPI / Card / Netbanking se pay karo.\n"
            f"Payment hote hi plan apne aap chalu ho jayega — yahin message aa jayega.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Pay karo", url=url)]]))
        return True

    if data == "pay_stars":
        if not STARS_ENABLED:
            await query.message.reply_text("⚠️ Stars payment abhi band hai.")
            return True
        try:
            await send_stars_invoice(context.bot, uid)
        except Exception as e:
            logger.error(f"Stars invoice fail: {e}")
            await query.message.reply_text("❌ Stars invoice nahi ban paya. Thodi der baad try karo.")
        return True

    if data == "open_plan":
        try:
            await query.edit_message_text(plan_text(uid), parse_mode=ParseMode.HTML,
                                          reply_markup=plan_kb(uid), disable_web_page_preview=True)
        except Exception:
            await query.message.reply_text(plan_text(uid), parse_mode=ParseMode.HTML,
                                           reply_markup=plan_kb(uid))
        return True
    return False

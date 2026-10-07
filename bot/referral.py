"""
referral.py — Refer & Earn (DealsKoti Auto Forwarder jaisa hi).

  • Har user ka ek chhota code: t.me/<bot>?start=ref_<CODE> (Telegram ID nahi dikhta)
  • Jab tak user ne EK BHI payment nahi kiya, jiske link se wo aakhri baar aaya
    wahi uska referrer (pehle se bot chala chuka user bhi). Pehla payment hote hi
    referrer HAMESHA ke liye fix — uske baad koi link nahi badal sakta
  • Referred user jab bhi payment kare (pehla + har renewal, Razorpay ya Stars) —
    referrer ko us payment ke ₹ ka REFERRAL_PERCENT% (default 20%). Stars mein
    payment ho to plan ki ₹ keemat ka 20%
  • Balance MIN_WITHDRAW (default ₹150) hone pe withdraw request — sirf UPI.
    Admin /withdrawals se Paid / Reject karta hai
  • Refer screen pe poori info: link, kitne aaye, kitne paid, kamai, balance,
    withdraw history, aur har referral ki list

Callback:  ref:home | ref:list:<page> | ref:wd | ref:wdgo | ref:pm | ref:pmset:<method>
           ref:hist | wdr:ok:<id> | wdr:no:<id> (admin)
"""
import html as html_lib
import logging
import os
import secrets

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

from storage import get_db, to_local
from ui import btn, tr, track, GREEN, BLUE, RED

logger = logging.getLogger(__name__)
esc = html_lib.escape


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


REFERRAL_PERCENT = _env_int("REFERRAL_PERCENT", 20)
MIN_WITHDRAW_PAISE = _env_int("MIN_WITHDRAW", 150) * 100
PAGE = 10

# Payout sirf UPI se
PAYOUT_METHODS = {
    "upi":    "🇮🇳 UPI",
}
PAYOUT_PROMPTS = {
    "upi":    ("Send your UPI ID.\nExample: <code>name@paytm</code>",
               "Apni UPI ID bhejein.\nJaise: <code>naam@paytm</code>"),
}


def money(paise: int) -> str:
    rupees = (paise or 0) / 100
    return f"₹{rupees:,.0f}" if rupees == int(rupees) else f"₹{rupees:,.2f}"


def _date(dt) -> str:
    return to_local(dt).strftime("%d %b %Y") if dt else "—"


# =============================================================================
# DATABASE
# =============================================================================
def init_tables():
    with get_db() as conn:
        with conn.cursor() as cur:
            for col in ("referral_code TEXT", "payout_method TEXT", "payout_address TEXT"):
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col}")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code_idx "
                        "ON users (referral_code) WHERE referral_code IS NOT NULL")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id          BIGSERIAL PRIMARY KEY,
                    referrer_id BIGINT    NOT NULL,
                    referred_id BIGINT    NOT NULL UNIQUE,
                    created_at  TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS referrals_referrer_idx ON referrals (referrer_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referral_credits (
                    id               BIGSERIAL PRIMARY KEY,
                    referrer_id      BIGINT    NOT NULL,
                    referred_id      BIGINT    NOT NULL,
                    amount_paise     INTEGER   NOT NULL,
                    commission_paise INTEGER   NOT NULL,
                    tier             TEXT,
                    source           TEXT,
                    payment_ref      TEXT      UNIQUE,
                    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS referral_credits_referrer_idx ON referral_credits (referrer_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id           BIGSERIAL PRIMARY KEY,
                    user_id      BIGINT    NOT NULL,
                    amount_paise INTEGER   NOT NULL,
                    method       TEXT      NOT NULL,
                    address      TEXT      NOT NULL,
                    status       TEXT      NOT NULL DEFAULT 'pending',
                    created_at   TIMESTAMP NOT NULL DEFAULT NOW(),
                    reviewed_by  BIGINT,
                    reviewed_at  TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS withdrawals_user_idx ON withdrawals (user_id, status)")


def ensure_code(uid: int) -> str:
    """User ka public referral code (pehli baar banta hai). Code share hota hai, ID nahi."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT referral_code FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0]
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # milte-julte akshar nahi
        for _ in range(8):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            try:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET referral_code = %s WHERE user_id = %s "
                                    "AND referral_code IS NULL RETURNING referral_code", (code, uid))
                        if cur.fetchone():
                            return code
                        cur.execute("SELECT referral_code FROM users WHERE user_id = %s", (uid,))
                        row = cur.fetchone()
                        return (row[0] if row else "") or ""
            except Exception:
                continue                                      # code takra gaya — doosra try
    except Exception as e:
        logger.error(f"ensure_code error: {e}")
    return ""


def uid_by_code(code: str):
    code = (code or "").strip().upper()
    if not code:
        return None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM users WHERE referral_code = %s", (code,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.error(f"uid_by_code error: {e}")
        return None


def link_referral(referrer_id: int, referred_id: int) -> bool:
    """
    Referral jodo / badlo. True = is referrer ke saath ab juda (naya ya badla).

      • Khud ko refer nahi kar sakte; referrer bot ka user hona chahiye
      • User ne abhi tak EK BHI payment nahi kiya → jiske link se aaya, wahi
        referrer (pehle kisi aur ke link se aaya tha to bhi badal jaata hai).
        Matlab jis link se wo pehla payment karega, commission ussi ko
      • Ek bhi payment ho gaya → referrer hamesha ke liye fix, koi link nahi badal sakta
    Row lock + payment check ek hi transaction mein, taaki payment ke saath-saath
    aaya link bhi referrer na badal paaye.
    """
    if not referrer_id or referrer_id == referred_id:
        return False
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM users WHERE user_id = %s", (referrer_id,))
                if not cur.fetchone():
                    return False
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (referred_id,))
                cur.execute("SELECT 1 FROM payments WHERE user_id = %s AND status = 'paid' LIMIT 1",
                            (referred_id,))
                if cur.fetchone():
                    return False                       # paying customer — referrer fix
                cur.execute("SELECT 1 FROM referral_credits WHERE referred_id = %s LIMIT 1", (referred_id,))
                if cur.fetchone():
                    return False                       # commission ban chuka — fix
                cur.execute(
                    """INSERT INTO referrals (referrer_id, referred_id) VALUES (%s, %s)
                       ON CONFLICT (referred_id) DO UPDATE
                       SET referrer_id = EXCLUDED.referrer_id, created_at = NOW()
                       WHERE referrals.referrer_id <> EXCLUDED.referrer_id
                       RETURNING id""", (referrer_id, referred_id))
                return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"link_referral error: {e}")
        return False


def referrer_of(uid: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (uid,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.error(f"referrer_of error: {e}")
        return None


def credit(referred_id: int, amount_paise: int, tier: str, source: str, payment_ref: str):
    """
    Referred user ke payment pe referrer ka commission. Har payment pe ek hi baar
    (payment_ref UNIQUE). Returns (referrer_id, commission_paise) ya None.
    """
    if amount_paise <= 0:
        return None
    commission = amount_paise * REFERRAL_PERCENT // 100
    if commission <= 0:
        return None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (referred_id,))
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (referred_id,))
                row = cur.fetchone()
                if not row:
                    return None
                cur.execute(
                    """INSERT INTO referral_credits
                       (referrer_id, referred_id, amount_paise, commission_paise, tier, source, payment_ref)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (payment_ref) DO NOTHING RETURNING id""",
                    (row[0], referred_id, amount_paise, commission, tier, source, payment_ref or None),
                )
                if cur.fetchone() is None:
                    return None
                return row[0], commission
    except Exception as e:
        logger.error(f"referral credit error: {e}")
        return None


def summary(uid: int) -> dict:
    out = {"joined": 0, "paid_users": 0, "earned": 0, "withdrawn": 0, "pending": 0,
           "available": 0, "month": 0, "payments": 0}
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s", (uid,))
                out["joined"] = cur.fetchone()[0]
                cur.execute(
                    """SELECT COUNT(DISTINCT referred_id), COALESCE(SUM(commission_paise), 0), COUNT(*),
                              COALESCE(SUM(commission_paise) FILTER (
                                  WHERE created_at >= date_trunc('month', NOW())), 0)
                       FROM referral_credits WHERE referrer_id = %s""", (uid,))
                r = cur.fetchone()
                out.update(paid_users=r[0], earned=int(r[1]), payments=r[2], month=int(r[3]))
                cur.execute(
                    """SELECT COALESCE(SUM(amount_paise) FILTER (WHERE status = 'paid'), 0),
                              COALESCE(SUM(amount_paise) FILTER (WHERE status = 'pending'), 0)
                       FROM withdrawals WHERE user_id = %s""", (uid,))
                w = cur.fetchone()
                out.update(withdrawn=int(w[0]), pending=int(w[1]))
    except Exception as e:
        logger.error(f"referral summary error: {e}")
    out["available"] = max(0, out["earned"] - out["withdrawn"] - out["pending"])
    return out


def referral_list(uid: int, page: int = 0):
    """(rows, total) — har referral: naam, kab aaya, plan, usse kitni kamai."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = %s", (uid,))
                total = cur.fetchone()[0]
                cur.execute(
                    """SELECT r.referred_id, u.first_name, u.username, r.created_at, u.expires_at,
                              u.is_trial, u.tier,
                              COALESCE(SUM(c.commission_paise), 0), COUNT(c.id)
                       FROM referrals r
                       LEFT JOIN users u ON u.user_id = r.referred_id
                       LEFT JOIN referral_credits c ON c.referred_id = r.referred_id AND c.referrer_id = r.referrer_id
                       WHERE r.referrer_id = %s
                       GROUP BY r.referred_id, u.first_name, u.username, r.created_at, u.expires_at,
                                u.is_trial, u.tier
                       ORDER BY r.created_at DESC
                       LIMIT %s OFFSET %s""", (uid, PAGE, page * PAGE))
                return cur.fetchall(), total
    except Exception as e:
        logger.error(f"referral_list error: {e}")
        return [], 0


def get_payout(uid: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT payout_method, payout_address FROM users WHERE user_id = %s", (uid,))
                row = cur.fetchone()
        # Purana method (USDT / Stars / Wallet) ab nahi chalta — set nahi maano, UPI daalna padega
        if not row or row[0] not in PAYOUT_METHODS or not row[1]:
            return None, None
        return row[0], row[1]
    except Exception as e:
        logger.error(f"get_payout error: {e}")
        return None, None


def set_payout(uid: int, method: str, address: str) -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET payout_method = %s, payout_address = %s WHERE user_id = %s",
                            (method, address, uid))
        return True
    except Exception as e:
        logger.error(f"set_payout error: {e}")
        return False


def pending_withdrawal(uid: int):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, amount_paise, method, address, created_at FROM withdrawals "
                            "WHERE user_id = %s AND status = 'pending' ORDER BY id DESC LIMIT 1", (uid,))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"pending_withdrawal error: {e}")
        return None


def create_withdrawal(uid: int, method: str, address: str):
    """Poora available balance ek request mein. Ek waqt mein ek hi pending request
    (warna same paise do baar maang lete). Returns (id, amount) ya None."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (uid,))   # isi user ke do request ek saath nahi
                cur.execute("SELECT 1 FROM withdrawals WHERE user_id = %s AND status = 'pending'", (uid,))
                if cur.fetchone():
                    return None
                cur.execute("SELECT COALESCE(SUM(commission_paise), 0) FROM referral_credits "
                            "WHERE referrer_id = %s", (uid,))
                earned = int(cur.fetchone()[0])
                cur.execute("SELECT COALESCE(SUM(amount_paise), 0) FROM withdrawals "
                            "WHERE user_id = %s AND status = 'paid'", (uid,))
                amount = earned - int(cur.fetchone()[0])
                if amount < MIN_WITHDRAW_PAISE:
                    return None
                cur.execute("INSERT INTO withdrawals (user_id, amount_paise, method, address) "
                            "VALUES (%s, %s, %s, %s) RETURNING id", (uid, amount, method, address))
                return cur.fetchone()[0], amount
    except Exception as e:
        logger.error(f"create_withdrawal error: {e}")
        return None


def withdrawal_history(uid: int, limit: int = 10) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, amount_paise, method, status, created_at, reviewed_at FROM withdrawals "
                            "WHERE user_id = %s ORDER BY id DESC LIMIT %s", (uid, limit))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"withdrawal_history error: {e}")
        return []


def list_pending_withdrawals(limit: int = 20) -> list:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT w.id, w.user_id, w.amount_paise, w.method, w.address, w.created_at,
                              u.first_name, u.username
                       FROM withdrawals w LEFT JOIN users u ON u.user_id = w.user_id
                       WHERE w.status = 'pending' ORDER BY w.created_at ASC LIMIT %s""", (limit,))
                return cur.fetchall()
    except Exception as e:
        logger.error(f"list_pending_withdrawals error: {e}")
        return []


def count_pending_withdrawals() -> int:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
                return cur.fetchone()[0]
    except Exception:
        return 0


def review_withdrawal(wid: int, status: str, admin_id: int):
    """Sirf 'pending' wali ko — do admin ek saath dabayein to bhi ek hi baar. Returns row ya None."""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""UPDATE withdrawals SET status = %s, reviewed_by = %s, reviewed_at = NOW()
                               WHERE id = %s AND status = 'pending'
                               RETURNING user_id, amount_paise, method, address""",
                            (status, admin_id, wid))
                return cur.fetchone()
    except Exception as e:
        logger.error(f"review_withdrawal error: {e}")
        return None


def admin_referral_line(uid: int) -> str:
    """Admin ke user card ke liye: kisne refer kiya + iske referrals ki kamai."""
    by = referrer_of(uid)
    s = summary(uid)
    parts = []
    if by:
        parts.append(f"🎁 Referred by: <code>{by}</code>")
    if s["joined"]:
        parts.append(f"👥 Referrals: {s['joined']} (paid {s['paid_users']}) • Kamai {money(s['earned'])} • "
                     f"Baaki {money(s['available'])}" + (f" • Pending {money(s['pending'])}" if s["pending"] else ""))
    return "\n".join(parts)


# =============================================================================
# START LINK + PAYMENT HOOK
# =============================================================================
async def handle_start_arg(bot, uid: int, arg: str):
    """/start ref_<CODE> — referral jodo aur referrer ko batao."""
    if not arg or not arg.startswith("ref_"):
        return
    raw = arg[4:].strip()
    referrer = int(raw) if raw.isdigit() else uid_by_code(raw)
    if not referrer or not link_referral(referrer, uid):
        return
    from alerts import who, notify_admins
    from engine import dm_user
    from users import get_lang
    lang = get_lang(referrer)
    await dm_user(bot, referrer,
                  tr(lang, f"🎉 <b>New referral joined!</b>\n👤 {who(uid)}\n\n"
                           f"You earn {REFERRAL_PERCENT}% of every payment they make — first payment and "
                           "every renewal. /refer",
                     f"🎉 <b>Naya referral aaya!</b>\n👤 {who(uid)}\n\n"
                     f"Ye jab bhi payment karenge — pehla aur har renewal — aapko {REFERRAL_PERCENT}% milega. /refer"),
                  parse_mode=ParseMode.HTML)
    await notify_admins(bot, f"🎁 <b>Referral</b>: {who(uid)} ← {who(referrer)}", uid)


async def on_payment(bot, referred_id: int, amount_paise: int, tier: str, source: str, payment_ref: str):
    """Har successful payment ke baad (billing.after_paid se). Referrer ko commission + khabar."""
    got = credit(referred_id, amount_paise, tier, source, payment_ref)
    if not got:
        return
    referrer, commission = got
    from alerts import who
    from engine import dm_user
    from users import get_lang
    lang = get_lang(referrer)
    s = summary(referrer)
    await dm_user(bot, referrer,
                  tr(lang, f"💰 <b>You earned {money(commission)}!</b>\n"
                           f"Your referral {who(referred_id)} paid {money(amount_paise)}.\n\n"
                           f"💼 Available balance: <b>{money(s['available'])}</b>\n"
                           f"🏆 Total earned: {money(s['earned'])}\n/refer",
                     f"💰 <b>Aapko {money(commission)} mile!</b>\n"
                     f"Aapke referral {who(referred_id)} ne {money(amount_paise)} ka payment kiya.\n\n"
                     f"💼 Balance: <b>{money(s['available'])}</b>\n"
                     f"🏆 Ab tak kul kamai: {money(s['earned'])}\n/refer"),
                  parse_mode=ParseMode.HTML)


# =============================================================================
# SCREENS
# =============================================================================
async def refer_screen(bot, uid: int, lang: str):
    me = await bot.get_me()
    code = ensure_code(uid) or str(uid)
    link = f"https://t.me/{me.username}?start=ref_{code}"
    s = summary(uid)
    pend = pending_withdrawal(uid)
    method, address = get_payout(uid)

    lines = [
        tr(lang, "🎁 <b>Refer & Earn</b>\n", "🎁 <b>Refer & Earn</b>\n"),
        tr(lang, f"Earn <b>{REFERRAL_PERCENT}% commission</b> on <b>every payment</b> your referrals make — "
                 "first payment and every renewal, for life (Stars payments count at the plan's ₹ price).\n"
                 "<i>Someone who hasn't paid yet becomes yours when they open your link; after their first "
                 "payment they stay yours forever.</i>\n",
           f"Aapke referral jab bhi payment karein — <b>pehla aur har renewal, hamesha</b> — aapko "
           f"<b>{REFERRAL_PERCENT}% commission</b> (Stars se payment ho to plan ki ₹ keemat ka).\n"
           "<i>Jisne abhi tak payment nahi kiya, wo aapke link se aaye to aapka. Pehla payment hote hi "
           "hamesha ke liye aapka.</i>\n"),
        tr(lang, "🔗 <b>Your link</b> (tap to copy):", "🔗 <b>Aapka link</b> (tap karke copy):"),
        f"<code>{esc(link)}</code>\n",
        tr(lang, "📊 <b>Your referrals</b>", "📊 <b>Aapke referrals</b>"),
        tr(lang, f"👥 Joined: <b>{s['joined']}</b>", f"👥 Jude: <b>{s['joined']}</b>"),
        tr(lang, f"💳 Paid users: <b>{s['paid_users']}</b>  ({s['payments']} payments)",
           f"💳 Paid users: <b>{s['paid_users']}</b>  ({s['payments']} payment)"),
        "",
        tr(lang, "💰 <b>Earnings</b>", "💰 <b>Kamai</b>"),
        tr(lang, f"🏆 Total earned: <b>{money(s['earned'])}</b>", f"🏆 Kul kamai: <b>{money(s['earned'])}</b>"),
        tr(lang, f"📅 This month: <b>{money(s['month'])}</b>", f"📅 Is mahine: <b>{money(s['month'])}</b>"),
        tr(lang, f"✅ Withdrawn: <b>{money(s['withdrawn'])}</b>", f"✅ Nikaala: <b>{money(s['withdrawn'])}</b>"),
    ]
    if s["pending"]:
        lines.append(tr(lang, f"⏳ Payout pending: <b>{money(s['pending'])}</b>",
                        f"⏳ Payout pending: <b>{money(s['pending'])}</b>"))
    lines.append(tr(lang, f"💼 Available: <b>{money(s['available'])}</b>",
                    f"💼 Balance: <b>{money(s['available'])}</b>"))
    lines.append("")
    lines.append(tr(lang, f"📌 Minimum payout: {money(MIN_WITHDRAW_PAISE)}",
                    f"📌 Kam se kam payout: {money(MIN_WITHDRAW_PAISE)}"))
    lines.append(tr(lang, "🏦 Payout method: ", "🏦 Payout method: ")
                 + (f"{PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>"
                    if method and address else tr(lang, "❌ not set", "❌ set nahi")))
    if pend:
        lines.append(tr(lang, f"\n⏳ Your payout request of {money(pend[1])} is being processed.",
                        f"\n⏳ Aapki {money(pend[1])} ki payout request process ho rahi hai."))

    share = (f"https://t.me/share/url?url={link}&text=" +
             "Make%20beautiful%20Amazon%20deal%20posts%20with%20your%20own%20affiliate%20tag%20%E2%80%94%20try%20it%20free!")
    rows = [
        [btn("💸 Withdraw", GREEN, callback_data="ref:wd"),
         btn(tr(lang, "🏦 Payout Method", "🏦 Payout Method"), callback_data="ref:pm")],
        [btn(tr(lang, "👥 My Referrals", "👥 Mere Referrals"), callback_data="ref:list:0"),
         btn(tr(lang, "🧾 Payout History", "🧾 Payout History"), callback_data="ref:hist")],
        [btn(tr(lang, "📤 Share Link", "📤 Link Share karein"), BLUE, url=share)],
        [btn("🔄 Refresh", callback_data="ref:home"), btn("🏠 Home", callback_data="home")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _plan_state(expires_at, is_trial, tier, lang) -> str:
    from storage import utcnow
    from tiers import TIERS
    if expires_at and expires_at > utcnow():
        if is_trial:
            return "🎁 Trial"
        t = TIERS.get(tier or "", {})
        return f"{t.get('emoji', '✅')} {t.get('name', 'Paid')}"
    if expires_at:
        return tr(lang, "⌛ Expired", "⌛ Khatam")
    return tr(lang, "🆕 No plan", "🆕 Plan nahi")


def list_screen(uid: int, lang: str, page: int = 0):
    rows_, total = referral_list(uid, page)
    lines = [tr(lang, f"👥 <b>My Referrals</b> ({total})\n", f"👥 <b>Mere Referrals</b> ({total})\n")]
    if not rows_:
        lines.append(tr(lang, "No one has joined with your link yet.\nShare it from /refer!",
                        "Abhi tak aapke link se koi nahi juda.\n/refer se link share karein!"))
    for i, (rid, name, uname, joined, exp, trial, tier, earned, pays) in enumerate(rows_, start=page * PAGE + 1):
        who_ = esc(name or "") + (f" @{esc(uname)}" if uname else "")
        lines.append(f"<b>{i}.</b> {who_.strip() or rid}")
        lines.append(tr(lang,
                        f"     🗓️ {_date(joined)} • {_plan_state(exp, trial, tier, lang)} • "
                        f"💳 {pays} • 💰 {money(earned)}",
                        f"     🗓️ {_date(joined)} • {_plan_state(exp, trial, tier, lang)} • "
                        f"💳 {pays} • 💰 {money(earned)}"))
    lines.append(tr(lang, "\n<i>💳 = payments made • 💰 = what you earned from them</i>",
                    "\n<i>💳 = kitne payment kiye • 💰 = unse aapki kamai</i>"))
    nav = []
    if page > 0:
        nav.append(btn("⬅️", callback_data=f"ref:list:{page - 1}"))
    if (page + 1) * PAGE < total:
        nav.append(btn("➡️", callback_data=f"ref:list:{page + 1}"))
    kb = ([nav] if nav else []) + [[btn("⬅️ Refer & Earn", callback_data="ref:home"),
                                    btn("🏠 Home", callback_data="home")]]
    return "\n".join(lines), InlineKeyboardMarkup(kb)


def history_screen(uid: int, lang: str):
    rows_ = withdrawal_history(uid)
    lines = [tr(lang, "🧾 <b>Payout History</b>\n", "🧾 <b>Payout History</b>\n")]
    icons = {"pending": "⏳", "paid": "✅", "rejected": "❌"}
    if not rows_:
        lines.append(tr(lang, "No payout requests yet.", "Abhi tak koi payout request nahi."))
    for wid, amount, method, status, created, reviewed in rows_:
        lines.append(f"{icons.get(status, '•')} <b>{money(amount)}</b> — {PAYOUT_METHODS.get(method, method)} — "
                     f"{_date(created)}" + (f" → {_date(reviewed)}" if reviewed else ""))
    return "\n".join(lines), InlineKeyboardMarkup([[btn("⬅️ Refer & Earn", callback_data="ref:home"),
                                                     btn("🏠 Home", callback_data="home")]])


def withdraw_screen(uid: int, lang: str):
    s = summary(uid)
    pend = pending_withdrawal(uid)
    method, address = get_payout(uid)
    back = [btn("⬅️ Refer & Earn", callback_data="ref:home")]
    if pend:
        return (tr(lang, f"💸 <b>Withdraw</b>\n\n⏳ You already have a request of <b>{money(pend[1])}</b> "
                         f"({PAYOUT_METHODS.get(pend[2], pend[2])}) pending since {_date(pend[4])}.\n"
                         "You'll be told when it is paid.",
                   f"💸 <b>Withdraw</b>\n\n⏳ Aapki <b>{money(pend[1])}</b> ki request "
                   f"({PAYOUT_METHODS.get(pend[2], pend[2])}) {_date(pend[4])} se pending hai.\n"
                   "Paise bhejte hi aapko bata diya jayega."),
                InlineKeyboardMarkup([back]))
    if s["available"] < MIN_WITHDRAW_PAISE:
        short = MIN_WITHDRAW_PAISE - s["available"]
        return (tr(lang, f"💸 <b>Withdraw</b>\n\n💼 Available: <b>{money(s['available'])}</b>\n"
                         f"📌 Minimum: {money(MIN_WITHDRAW_PAISE)}\n\n"
                         f"⚠️ You need <b>{money(short)}</b> more. Share your link to earn faster!",
                   f"💸 <b>Withdraw</b>\n\n💼 Balance: <b>{money(s['available'])}</b>\n"
                   f"📌 Kam se kam: {money(MIN_WITHDRAW_PAISE)}\n\n"
                   f"⚠️ Abhi <b>{money(short)}</b> aur chahiye. Link share karein, jaldi kamaayein!"),
                InlineKeyboardMarkup([back]))
    if not method or not address:
        return (tr(lang, f"💸 <b>Withdraw</b>\n\n💼 Available: <b>{money(s['available'])}</b>\n\n"
                         "🏦 Set a payout method first.",
                   f"💸 <b>Withdraw</b>\n\n💼 Balance: <b>{money(s['available'])}</b>\n\n"
                   "🏦 Pehle payout method set karein."),
                InlineKeyboardMarkup([[btn(tr(lang, "🏦 Set Payout Method", "🏦 Payout Method set karein"),
                                           BLUE, callback_data="ref:pm")], back]))
    return (tr(lang, f"💸 <b>Withdraw</b>\n\n💼 Amount: <b>{money(s['available'])}</b>\n"
                     f"🏦 To: {PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>\n\nSend the request?",
               f"💸 <b>Withdraw</b>\n\n💼 Rakam: <b>{money(s['available'])}</b>\n"
               f"🏦 Kahan: {PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>\n\nRequest bhejein?"),
            InlineKeyboardMarkup([[btn(tr(lang, "✅ Request Payout", "✅ Request bhejein"), GREEN,
                                       callback_data="ref:wdgo")],
                                  [btn(tr(lang, "🏦 Change Method", "🏦 Method badlein"), callback_data="ref:pm")],
                                  back]))


def method_screen(uid: int, lang: str):
    method, address = get_payout(uid)
    cur = (f"{PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>"
           if method and address else tr(lang, "❌ not set", "❌ set nahi"))
    rows = [[btn(("✏️ Change " if method else "➕ Add ") + label, BLUE, callback_data=f"ref:pmset:{k}")]
            for k, label in PAYOUT_METHODS.items()]
    rows.append([btn("⬅️ Refer & Earn", callback_data="ref:home")])
    return (tr(lang, f"🏦 <b>Payout Method</b>\n\nCurrent: {cur}\n\nPayouts are sent to your <b>UPI ID</b>.",
               f"🏦 <b>Payout Method</b>\n\nAbhi: {cur}\n\nPayout aapki <b>UPI ID</b> pe bheja jaata hai."),
            InlineKeyboardMarkup(rows))


# =============================================================================
# CALLBACKS / INPUT / ADMIN
# =============================================================================
async def handle_callback(query, context, uid: int, data: str) -> bool:
    if not (data.startswith("ref:") or data.startswith("wdr:")):
        return False
    from task_ui import show
    from users import get_lang, is_admin
    lang = get_lang(uid)
    p = data.split(":")

    if p[0] == "wdr":
        if not is_admin(uid):
            await query.answer("Sirf admin ke liye.", show_alert=True)
            return True
        await review_callback(query, context, uid, p)
        return True

    act = p[1] if len(p) > 1 else "home"
    if act == "home":
        context.user_data.pop("action", None)
        text, kb = await refer_screen(context.bot, uid, lang)
        await show(query, context, text, kb)
    elif act == "list":
        page = int(p[2]) if len(p) > 2 and p[2].isdigit() else 0
        text, kb = list_screen(uid, lang, page)
        await show(query, context, text, kb)
    elif act == "hist":
        text, kb = history_screen(uid, lang)
        await show(query, context, text, kb)
    elif act == "wd":
        text, kb = withdraw_screen(uid, lang)
        await show(query, context, text, kb)
    elif act == "wdgo":
        method, address = get_payout(uid)
        if not method or not address:
            await query.answer(tr(lang, "Set a payout method first.", "Pehle payout method set karein."),
                               show_alert=True)
            return True
        res = create_withdrawal(uid, method, address)
        if not res:
            text, kb = withdraw_screen(uid, lang)          # balance kam / pehle se pending — wahi dikhao
            await show(query, context, text, kb)
            return True
        wid, amount = res
        from alerts import notify_admins, who
        await notify_admins(context.bot,
                            f"💸 <b>Payout request #{wid}</b>\n👤 {who(uid)}\n💰 <b>{money(amount)}</b>\n"
                            f"🏦 {PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>\n\n"
                            "Review: /withdrawals")
        # Report — delete nahi hota
        await query.message.reply_text(
            tr(lang, f"✅ <b>Payout requested!</b>\n💰 {money(amount)} → {PAYOUT_METHODS.get(method, method)}\n"
                     "You'll be told as soon as it is paid.",
               f"✅ <b>Payout request bhej di!</b>\n💰 {money(amount)} → {PAYOUT_METHODS.get(method, method)}\n"
               "Paise bhejte hi aapko bata diya jayega."), parse_mode=ParseMode.HTML)
        text, kb = await refer_screen(context.bot, uid, lang)
        await show(query, context, text, kb)
    elif act == "pm":
        text, kb = method_screen(uid, lang)
        await show(query, context, text, kb)
    elif act == "pmset" and len(p) > 2 and p[2] in PAYOUT_METHODS:
        context.user_data.update(action="ref_pm", ref_method=p[2])
        en, hi = PAYOUT_PROMPTS[p[2]]
        await show(query, context, f"🏦 <b>{PAYOUT_METHODS[p[2]]}</b>\n\n" + tr(lang, en, hi),
                   InlineKeyboardMarkup([[btn(tr(lang, "❌ Cancel", "❌ Cancel"), callback_data="ref:pm")]]))
    return True


async def handle_input(update, context, uid: int, action: str) -> bool:
    if action != "ref_pm":
        return False
    from users import get_lang
    lang = get_lang(uid)
    msg = update.message
    track(context, msg)
    raw = (msg.text or "").strip()
    method = context.user_data.get("ref_method")
    if method not in PAYOUT_METHODS:
        context.user_data.pop("action", None)
        return True
    if not (3 <= len(raw) <= 200) or "\n" in raw:
        track(context, await msg.reply_text(tr(lang, "⚠️ That doesn't look right. Please send it again.",
                                               "⚠️ Ye sahi nahi lag raha. Dobara bhejein.")))
        return True
    if method == "upi" and "@" not in raw:
        track(context, await msg.reply_text(tr(lang, "⚠️ A UPI ID has an @, like name@paytm. Send again.",
                                               "⚠️ UPI ID mein @ hota hai, jaise naam@paytm. Dobara bhejein.")))
        return True
    context.user_data.pop("action", None)
    context.user_data.pop("ref_method", None)
    set_payout(uid, method, raw)
    m = await msg.reply_text(
        tr(lang, f"✅ <b>Payout method saved</b>\n{PAYOUT_METHODS[method]} — <code>{esc(raw)}</code>",
           f"✅ <b>Payout method save ho gaya</b>\n{PAYOUT_METHODS[method]} — <code>{esc(raw)}</code>"),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[btn("💸 Withdraw", GREEN, callback_data="ref:wd"),
                                            btn("⬅️ Refer & Earn", callback_data="ref:home")]]))
    track(context, m)
    return True


def _admin_wd_text(row) -> str:
    wid, user_id, amount, method, address, created, first, uname = row
    who_ = esc(first or "") + (f" @{esc(uname)}" if uname else "")
    s = summary(user_id)
    return (f"💸 <b>Payout request #{wid}</b>\n"
            f"👤 {who_.strip()} <code>{user_id}</code>\n"
            f"💰 <b>{money(amount)}</b>\n"
            f"🏦 {PAYOUT_METHODS.get(method, method)} — <code>{esc(address)}</code>\n"
            f"🗓️ {to_local(created).strftime('%d %b %Y, %I:%M %p')}\n"
            f"📊 Referrals {s['joined']} • paid {s['paid_users']} • kul kamai {money(s['earned'])}")


def _admin_wd_kb(wid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("✅ Paid kar diya", GREEN, callback_data=f"wdr:ok:{wid}"),
                                  btn("❌ Reject", RED, callback_data=f"wdr:no:{wid}")]])


async def cmd_withdrawals(update, context, uid: int):
    rows_ = list_pending_withdrawals()
    if not rows_:
        track(context, await update.message.reply_text("✅ Koi payout request pending nahi hai."))
        return
    for row in rows_:
        track(context, await update.message.reply_text(_admin_wd_text(row), parse_mode=ParseMode.HTML,
                                                       reply_markup=_admin_wd_kb(row[0])))


async def review_callback(query, context, admin_id: int, p: list):
    if len(p) < 3 or not p[2].isdigit():
        return
    status = "paid" if p[1] == "ok" else "rejected"
    row = review_withdrawal(int(p[2]), status, admin_id)
    if not row:
        await query.answer("Ye request pehle hi handle ho chuki hai.", show_alert=True)
        return
    user_id, amount, method, address = row
    from engine import dm_user
    from users import get_lang
    lang = get_lang(user_id)
    if status == "paid":
        note = f"✅ Paid — {money(amount)}"
        await dm_user(context.bot, user_id,
                      tr(lang, f"💸 <b>Payout sent!</b>\n💰 {money(amount)} → {PAYOUT_METHODS.get(method, method)}\n"
                               f"<code>{esc(address)}</code>\n\nThank you for sharing! 🙏",
                         f"💸 <b>Payout bhej diya!</b>\n💰 {money(amount)} → {PAYOUT_METHODS.get(method, method)}\n"
                         f"<code>{esc(address)}</code>\n\nShare karne ke liye shukriya! 🙏"),
                      parse_mode=ParseMode.HTML)
    else:
        note = "❌ Reject — balance user ke paas wapas"
        await dm_user(context.bot, user_id,
                      tr(lang, "❌ <b>Payout request rejected.</b>\nYour balance is back in your account. "
                               "Contact support for details. /paysupport",
                         "❌ <b>Payout request reject hui.</b>\nAapka balance wapas aapke account mein hai. "
                         "Detail ke liye support se baat karein. /paysupport"),
                      parse_mode=ParseMode.HTML)
    try:
        await query.edit_message_text((query.message.text_html or "") + f"\n\n{note}", parse_mode=ParseMode.HTML)
    except Exception:
        await query.answer(note[:60])

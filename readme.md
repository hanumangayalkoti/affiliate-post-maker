# Deal Post Maker Bot

DealsKoti Master Bot ka public version. Koi bhi user apna Amazon affiliate tag aur
apna channel set karke Amazon links ko sundar deal posts mein badal sakta hai.
Plan paid hai (₹100 / 30 din), admin ke paas poora control hai.

## Kya-kya hai

**Purane saare features (har user ke liye alag):** DM se post, draft channel se auto
pickup, multi-link = alag-alag post, search page skip, 24 ghante duplicate check,
park mode (har ghante batch), detailed/minimal post, har field on/off, header,
footer, watermark, Buy Now / Add to Cart / 2 custom buttons, silent posting.

**Naya:**
- Har user ka apna affiliate tag — har link aur button mein usi ka tag lagta hai
- Channel jodna aasaan: bot ko channel mein admin banao, bot khud poochta hai
  (user us channel ka admin hona chahiye — koi dusre ka channel nahi le sakta)
- Search Links on/off (har user apne liye)
- Price Drop Alerts: `/track link [target]` — price gire to message, ya seedha
  channel pe post (Auto-Post setting)
- Stats: aaj / 7 din / 30 din / total posts
- Plan: Razorpay (UPI/Card, automatic) + Telegram Stars (automatic)
- Plan khatam hone se 2 din pehle aur khatam hone pe reminder
- Admin panel: stats, revenue, user search, din jodo/kaato, plan khatam, block,
  user ko message, broadcast
- Amazon API bachat: product data 30 minute cache — 10 channel same deal daalein
  to API sirf 1 baar call hoti hai. Data 5 din tak DB mein rehta hai.
- Daily post limit (default 300/din) taaki koi API ko over-use na kare

## Environment Variables (Railway)

| Variable | Zaroori | Kya hai |
|---|---|---|
| BOT_TOKEN | ✅ | Bot token |
| ADMIN_ID | ✅ | Tumhara Telegram ID |
| DATABASE_URL | ✅ | PostgreSQL (purana hi) |
| CREDENTIAL_ID | ✅ | Amazon Creators API |
| CREDENTIAL_SECRET | ✅ | Amazon Creators API |
| PARTNER_TAG | ✅ | Tumhara tag (API ke liye + tumhari posts ke liye) |
| CREDENTIAL_VERSION | ❌ | default 3.2 |
| MARKETPLACE | ❌ | default www.amazon.in |
| ADMIN_IDS | ❌ | Aur admins, comma se (jaise `123,456`) |
| BOT_NAME | ❌ | default "Deal Post Maker" |
| SUPPORT_USERNAME | ❌ | Support ke liye username (bina @ bhi chalega) |
| RAZORPAY_KEY_ID | ❌ | Razorpay key — teeno Razorpay wale na ho to UPI button chhupa rahega |
| RAZORPAY_KEY_SECRET | ❌ | Razorpay secret |
| RAZORPAY_WEBHOOK_SECRET | ❌ | Webhook banate waqt jo secret daala |
| RAZORPAY_WEBHOOK_PATH | ❌ | default /webhooks/razorpay |
| STARS_ENABLED | ❌ | default true |
| PRICE_INR | ❌ | default 100 |
| PRICE_STARS | ❌ | default 90 |
| PLAN_DAYS | ❌ | default 30 |
| DAILY_POST_LIMIT | ❌ | default 300 (0 = koi limit nahi) |
| CACHE_FRESH_MINUTES | ❌ | default 30 |
| CACHE_KEEP_DAYS | ❌ | default 5 |
| PRICE_CHECK_MINUTES | ❌ | default 60 |
| MIN_DROP_PCT | ❌ | default 5 (itna % gire tab alert) |
| MAX_WATCHES | ❌ | default 25 products per user |
| WATCH_DAYS | ❌ | default 30 din baad tracking band |
| TZ_NAME | ❌ | default Asia/Kolkata |

## Razorpay webhook setup (ek baar)

1. Railway → service → Settings → Networking → **Generate Domain**
2. Razorpay Dashboard → Settings → Webhooks → **Add New Webhook**
   - URL: `https://<railway-domain>/webhooks/razorpay`
   - Secret: kuch bhi strong (yahi `RAZORPAY_WEBHOOK_SECRET` mein daalo)
   - Events: `payment_link.paid` aur `payment.captured`
3. Auto Forwarder bot wala webhook bhi chalta rahega — dono apne-apne payment
   pehchaan lete hain, ek-dusre ka ignore karte hain.

## Telegram Stars

Kuch setup nahi. Stars bot ke balance mein aate hain — @BotFather → bot →
Payments / Stars balance se Fragment pe withdraw.

## Purana data

Pehli baar start hote hi purani admin config (channel, draft, header, watermark,
buttons) apne aap admin (ADMIN_ID) ke account mein chali jaati hai. Admin ko plan
ki zaroorat nahi.

## Files

```
main.py         — start, menu, help, DM / channel handlers, jobs
engine.py       — posting engine
settings_ui.py  — user settings
billing.py      — plan, Razorpay, Stars, webhook server
admin.py        — admin panel
price_watch.py  — price drop alerts
users.py        — users, plan expiry, payments
storage.py      — database tables + user settings
database.py     — duplicate, queue, cache, stats, price watch
amazon_api.py   — Amazon API + cache + user tag links
caption.py      — post caption
watermark.py    — photo watermark
```

`classifier.py` aur `keywords.py` kahin use nahi hote — repo se hata sakte ho.

Start command: `python main.py`

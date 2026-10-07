# Deal Post Maker Bot

*Made by Affiliates, for Affiliates*

Telegram bot jo Amazon (aur baaki) deals ko sundar posts mein badal ke aapke
channel pe daalta hai — har link pe user ka apna affiliate tag. English aur
Hinglish dono mein, "aap" wali izzat ke saath.

## Plans

| | 🥉 Basic | 🥈 Pro | 🥇 Premium |
|---|---|---|---|
| Price / 30 din | ₹50 | ₹100 | ₹250 |
| Tasks | 1 | 2 | 5 |
| Posts / din (har task) | 500 | 1500 | 5000 |
| Image Card | ❌ | ✅ | ✅ |

- Naye user ko **7 din Pro free** (ek hi baar)
- Plan badalne pe bache din ki keemat naye plan mein judti hai
- Chhote plan pe extra tasks **pause** hote hain (delete nahi) — user chunta hai kaunsa chale
- Payment: Razorpay (UPI/Card, automatic) + Telegram Stars (INR ke barabar)
- Plan / trial expiry, daily limit reset, reminders — sab **raat 12 baje (IST)**

## Features

- **Gate:** pehle language (English / Hinglish), phir "📢 Join Update Channel"
- **Tasks:** har task = 1 Draft ➜ 1 Destination + apni saari settings. ⭐ Default
  task DM wali deals sambhalta hai. Same channel do tasks mein ho ya ek ka
  Destination doosre ka Draft ho to bot user ko batata hai (rokta nahi)
- **Har task ki settings:** affiliate tag, Destination, Draft, Kaun Si Posts
  (🛍️ Amazon / 📝 Non-Amazon), ♻️ Duplicate check (Amazon = product, baaki =
  caption ya photo), post details, buttons (har button ka rang), header/footer,
  notification, search links
- **🎨 Image Card** (Pro+): product photo + Offer Price + Discount + MRP + Rating;
  size, photo size/side, theme, 4 font, badge shape, rang; 👁️ Preview, ↩️ Reset
- **💧 Watermark:** text, jagah (upar beech / neeche right / left / beech),
  size (Small / Medium / Large), rang
- **🛒 Amazon Logo:** Amazon post ki photo ke upar-left kone mein chhota official
  "available at amazon" badge (`assets/amazon_badge.png`) — har task mein ON/OFF, default ON
- **Chat clean:** naya command aane pe pichle commands aur unke jawab delete.
  Reports (payment, task bana, post report) kabhi delete nahi
- **FAQ:** 16 aam sawaal, number buttons se
- **🎁 Refer & Earn:** /refer — har referral ke har payment pe commission (REFERRAL_PERCENT, default 20%), MIN_WITHDRAW (default ₹150) pe UPI payout; pehle payment tak jiska link aakhri, wahi referrer — phir hamesha ke liye; admin /withdrawals se Paid / Reject
- **Admin panel (inline):** users ki list (filter + pages + 1-10 number), user
  card (plan, tasks, din jodo/kaato, tier badlo, block, message), payments,
  broadcast, Amazon API test. Admin ko har zaroori cheez ki khabar
- Saari settings PostgreSQL mein — crash / restart / redeploy pe kuch nahi khota

## Environment Variables (Railway)

| Variable | Zaroori | Kya hai |
|---|---|---|
| BOT_TOKEN | ✅ | Bot token |
| ADMIN_ID | ✅ | Aapka Telegram ID |
| DATABASE_URL | ✅ | PostgreSQL |
| CREDENTIAL_ID / CREDENTIAL_SECRET | ✅ | Amazon Creators API |
| PARTNER_TAG | ✅ | Aapka tag (API ke liye) |
| FORCE_JOIN_CHANNEL | ❌ | Jaise `@dealskoti` — khaali = gate mein join band |
| FORCE_JOIN_NAME | ❌ | Join message mein channel ka naam |
| FORCE_JOIN_LINK | ❌ | Private channel ka invite link |
| RAZORPAY_KEY_ID / KEY_SECRET / WEBHOOK_SECRET | ❌ | Teeno na hon to UPI button chhupa |
| STARS_ENABLED | ❌ | default true |
| PRICE_BASIC / PRO / PREMIUM | ❌ | default 50 / 100 / 250 |
| STARS_BASIC / PRO / PREMIUM | ❌ | default 50 / 100 / 250 |
| DAILY_BASIC / PRO / PREMIUM | ❌ | default 500 / 1500 / 5000 |
| PLAN_DAYS / TRIAL_DAYS | ❌ | default 30 / 7 |
| ADMIN_IDS | ❌ | Aur admins, comma se |
| BOT_NAME / SUPPORT_USERNAME | ❌ | |
| CACHE_FRESH_MINUTES / CACHE_KEEP_DAYS / TZ_NAME | ❌ | 30 / 5 / Asia/Kolkata |

## Razorpay webhook

Razorpay Dashboard → Webhooks → URL: `https://<railway-domain>` (sirf domain ya
`/webhooks/razorpay`), event `payment_link.paid`, secret = `RAZORPAY_WEBHOOK_SECRET`.

## Folders

```
bot/                 — saara Python code (start: python bot/main.py)
  main.py            — start, gate, home, DM / Draft handlers, raat 12 baje ka job
  task_ui.py         — tasks aur har task ki settings
  card_ui.py         — Image Card settings (har task ka)
  card.py            — image card + watermark drawing
  engine.py          — posting engine (task + tier ke hisaab se)
  billing.py         — plans, Razorpay, Stars, webhook server
  admin.py           — inline admin panel
  faq.py             — 15 FAQ
  gate.py            — language + force join
  tiers.py           — plans, limits, upgrade credit, raat 12 baje ka hisaab
  users.py           — users, plan, trial, payments
  storage.py         — database tables, tasks, purane data ka migration
  database.py        — duplicate, cache, stats
  alerts.py          — admin ko khabar
  ui.py              — rangeen buttons, language, chat clean
  amazon_api.py      — Amazon API + cache + user tag links
  caption.py         — post caption
  watermark.py       — normal photo pe watermark
assets/fonts/        — card ke fonts (OFL / Apache license)
railway.json         — Railway start command + restart policy
```

Start command: `python bot/main.py` (railway.json mein set hai)

"""
faq.py — 16 aam sawaal (English + Hinglish). Help → FAQ mein number buttons.
Callback:  faq  |  faq:<n>
"""
from telegram import InlineKeyboardMarkup

from tiers import TIERS, TRIAL_DAYS, PLAN_DAYS
from ui import btn, tr


def _plans(lang):
    return "\n".join(
        f"{t['emoji']} <b>{t['name']}</b> ₹{t['inr']} — {t['tasks']} task, {t['daily']} "
        f"{tr(lang, 'posts/day per task', 'post/din har task')}, Image Card {'✅' if t['card'] else '❌'}"
        for t in TIERS.values())


def _faqs(lang):
    return [
        (tr(lang, "How do I use the bot?", "Bot kaise use karein?"),
         tr(lang,
            "1️⃣ Open /tasks → your task\n2️⃣ Set your <b>Affiliate Tag</b>\n3️⃣ Set the <b>Destination</b> "
            "channel (make the bot an admin there)\n4️⃣ Send any Amazon link to the bot — it posts a "
            "neat deal in your channel with your tag.",
            "1️⃣ /tasks → apna task kholein\n2️⃣ <b>Affiliate Tag</b> set karein\n3️⃣ <b>Destination</b> "
            "channel set karein (bot ko wahan admin banayein)\n4️⃣ Bot ko koi bhi Amazon link bhejein — "
            "wo aapke tag ke saath sundar deal post channel pe daal dega.")),
        (tr(lang, "What is an affiliate tag? Where do I find it?", "Affiliate tag kya hai, kahan milega?"),
         tr(lang,
            "It's your Amazon Associates ID, like <code>mydeals-21</code>. Every link gets it so the "
            "commission comes to you. Log in at affiliate-program.amazon.in — it's in the top-right corner.",
            "Ye aapka Amazon Associates ID hai, jaise <code>mydeals-21</code>. Har link mein lagta hai "
            "taaki kamai aapko mile. affiliate-program.amazon.in pe login karein — upar right corner mein hota hai.")),
        (tr(lang, "How do I add my channel?", "Channel kaise jodein?"),
         tr(lang,
            "Make the bot an <b>admin</b> in your channel with 'Post Messages' ON. The bot will message "
            "you and ask how to use it — just tap a button. Or in the task, tap 📢 Destination and send "
            "the @username.",
            "Bot ko apne channel mein <b>admin</b> banayein ('Post Messages' ON). Bot aapko khud message "
            "karke poochega — bas button dabayein. Ya task mein 📢 Destination dabake @username bhejein.")),
        (tr(lang, "What is a Draft channel?", "Draft channel kya hai?"),
         tr(lang,
            "A channel where you put deals. The bot picks them up and posts them to your Destination "
            "with your tag. It's optional — you can also send deals to the bot in DM.",
            "Ek channel jisme aap deals daalte hain. Bot wahan se utha ke aapke tag ke saath Destination "
            "pe post karta hai. Ye optional hai — bot ko DM mein bhi deal bhej sakte hain.")),
        (tr(lang, "What is a Task?", "Task kya hai?"),
         tr(lang,
            "A Task = 1 Draft channel ➜ 1 Destination channel, with its own settings (tag, watermark, "
            "card, buttons...). The ⭐ Default task handles deals you send in DM.",
            "Task = 1 Draft channel ➜ 1 Destination channel, apni settings ke saath (tag, watermark, "
            "card, buttons...). ⭐ Default task DM mein bheji deals sambhalta hai.")),
        (tr(lang, "What's the difference between plans?", "Plans mein kya fark hai?"),
         _plans(lang) + tr(lang, f"\n\nEach plan is for {PLAN_DAYS} days. All other features are in every plan.",
                           f"\n\nHar plan {PLAN_DAYS} din ka. Baaki saare features har plan mein.")),
        (tr(lang, "How do I get the free trial?", "Free trial kaise milega?"),
         tr(lang,
            f"Every new user gets <b>{TRIAL_DAYS} days of Pro free</b> automatically after joining. "
            "It's given only once per account.",
            f"Har naye user ko join karte hi <b>{TRIAL_DAYS} din Pro free</b> milta hai. Ye har account ko "
            "ek hi baar milta hai.")),
        (tr(lang, "I paid but my plan didn't start?", "Payment ho gayi par plan chalu nahi hua?"),
         tr(lang,
            "It usually starts within a minute. If not, use /paysupport and send your payment "
            "screenshot — we'll fix it quickly.",
            "Aam taur pe 1 minute mein chalu ho jaata hai. Na ho to /paysupport pe payment ka "
            "screenshot bhejein — jaldi theek kar denge.")),
        (tr(lang, "Why didn't my post go to the channel?", "Post channel mein kyun nahi gayi?"),
         tr(lang,
            "Check: ① the bot is an admin with 'Post Messages' ON ② the task has a Tag and Destination "
            "③ the task is ▶️ running ④ today's limit isn't used up ⑤ 'Which Posts' allows that type ⑥ "
            "it isn't a duplicate. The bot always replies with the reason.",
            "Check karein: ① bot admin hai aur 'Post Messages' ON ② task mein Tag aur Destination set "
            "③ task ▶️ chal raha hai ④ aaj ki limit bachi hai ⑤ 'Kaun Si Posts' mein wo type ON hai "
            "⑥ duplicate nahi hai. Bot hamesha wajah batata hai.")),
        (tr(lang, "What is the Image Card?", "Image Card kya hai?"),
         tr(lang,
            "It turns an Amazon deal into a clean picture — product photo with Offer Price, Discount "
            "and MRP, in your colours and font. Open your task → 🎨 Image Card → turn ON → 👁️ Preview. "
            "Available on Pro and Premium.",
            "Amazon deal ko ek saaf photo mein badal deta hai — product photo ke saath Offer Price, "
            "Discount aur MRP, aapke rang aur font mein. Task → 🎨 Image Card → ON → 👁️ Preview. "
            "Pro aur Premium mein milta hai.")),
        (tr(lang, "How do I add a watermark?", "Watermark kaise lagayein?"),
         tr(lang,
            "Task → 💧 Watermark → send your text (like @MyDeals) → choose position, size and colour. "
            "It goes on every post photo.",
            "Task → 💧 Watermark → apna text bhejein (jaise @MyDeals) → jagah, size aur rang chunein. "
            "Har post ki photo pe lagega.")),
        (tr(lang, "How do I stop duplicate posts?", "Duplicate post kaise rokein?"),
         tr(lang,
            "Task → ♻️ Duplicate → ON. The same Amazon product, or the same caption (or same photo), "
            "won't be posted again within 24 hours.",
            "Task → ♻️ Duplicate → ON. Same Amazon product, ya same caption (ya same photo) 24 ghante "
            "mein dobara post nahi hoga.")),
        (tr(lang, "What is the daily limit? When does it reset?", "Daily limit kya hai, kab reset hoti hai?"),
         tr(lang,
            "It's the number of posts per day for your plan (see /plan). The limit is for EACH task "
            "separately — e.g. on Pro (500) with 2 tasks, each task can post 500 a day. It resets every "
            "night at 12:00 midnight (IST).",
            "Aapke plan mein roz kitni post ho sakti hain (/plan dekhein). Ye limit HAR TASK ki alag hai — "
            "jaise Pro (500) mein 2 task hain to dono task roz 500-500 post kar sakte hain. Har raat 12 baje "
            "(IST) reset hoti hai.")),
        (tr(lang, "How does Refer & Earn work?", "Refer & Earn kaise kaam karta hai?"),
         tr(lang,
            "Open /refer and share your link. Whenever someone who joined with it pays — first time and every "
            "renewal — you get a commission of that payment. Once your balance reaches the minimum, tap "
            "💸 Withdraw (UPI / USDT / Stars / Telegram Wallet). /refer also shows every referral and your "
            "payout history.",
            "/refer kholein aur apna link share karein. Us link se juda koi bhi jab payment kare — pehli baar "
            "aur har renewal pe — aapko us payment ka commission milta hai. Balance minimum tak pahunche to "
            "💸 Withdraw dabayein (UPI / USDT / Stars / Telegram Wallet). /refer mein har referral aur payout "
            "history bhi dikhti hai.")),
        (tr(lang, "What happens if I upgrade or downgrade?", "Plan upgrade/downgrade karne pe kya hoga?"),
         tr(lang,
            "The value of your remaining days is added to the new plan — nothing is lost. On a smaller "
            "plan, extra tasks are paused (not deleted); you choose which ones run.",
            "Bache din ki keemat naye plan mein jud jaati hai — kuch nahi kat-ta. Chhote plan pe extra "
            "tasks pause hote hain (delete nahi); kaunsa chalana hai aap chunte hain.")),
        (tr(lang, "How do I contact support?", "Support se kaise baat karein?"),
         tr(lang, "Use /paysupport — it shows your ID and the support contact.",
            "/paysupport dabayein — wahan aapka ID aur support ka contact hai.")),
    ]


def faq_text(lang: str) -> str:
    qs = _faqs(lang)
    lines = [tr(lang, "❓ <b>FAQ — Common Questions</b>\n", "❓ <b>FAQ — Aam Sawaal</b>\n")]
    lines += [f"{i}. {q}" for i, (q, _) in enumerate(qs, 1)]
    lines.append(tr(lang, "\n<i>Tap a number 👇</i>", "\n<i>Number dabayein 👇</i>"))
    return "\n".join(lines)


def faq_kb(lang: str) -> InlineKeyboardMarkup:
    n = len(_faqs(lang))
    nums = [btn(str(i), callback_data=f"faq:{i}") for i in range(1, n + 1)]
    rows = [nums[i:i + 5] for i in range(0, n, 5)]
    rows.append([btn(tr(lang, "⬅️ Help", "⬅️ Help"), callback_data="menu_help"),
                 btn("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def answer_text(lang: str, i: int) -> str:
    qs = _faqs(lang)
    if not 1 <= i <= len(qs):
        return faq_text(lang)
    q, a = qs[i - 1]
    return f"❓ <b>{i}. {q}</b>\n\n{a}"


def answer_kb(lang: str, i: int) -> InlineKeyboardMarkup:
    n = len(_faqs(lang))
    nav = []
    if i > 1:
        nav.append(btn("⬅️", callback_data=f"faq:{i - 1}"))
    nav.append(btn(tr(lang, "📋 All Questions", "📋 Saare Sawaal"), callback_data="faq"))
    if i < n:
        nav.append(btn("➡️", callback_data=f"faq:{i + 1}"))
    return InlineKeyboardMarkup([nav])

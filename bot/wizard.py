"""
wizard.py — naye user ka 🧭 Setup Wizard (Draft → Destination → Amazon Tag → Ready).

- Har step ki shuruaat mein: ye step kya karta hai, zaroori hai ya nahi, aur kyun.
- Ek hi message badalta rehta hai (chat saaf). Har step pe "⏭️ Baad mein".
- Naye user ko apne aap ek baar dikhta hai; skip kiya to /start pe "🧭 Setup" button —
  jab tak koi task poora set na ho ya forwarding shuru na ho jaaye.
- Aakhir mein (sab set ho to) Destination mein ek sample deal user ke tag ke saath.

Callbacks: wz:go:<step> | wz:set:<src|dest|tag> | wz:sample | wz:close
"""
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

from database import user_stats
from engine import day_start_naive, make_affiliate_url
from storage import list_tasks, create_task, get_task, new_task_config
from ui import btn, tr, chan, GREEN, BLUE
from users import get_lang, set_default_task, is_admin

logger = logging.getLogger(__name__)
esc = html_lib.escape

STEPS = ("src", "dest", "tag", "done")
SAMPLE_ASIN = "B08N5WRWNW"


# =============================================================================
# KAB DIKHE
# =============================================================================
def task_complete(cfg: dict) -> bool:
    """Draft + Destination + (Amazon ON ho to) Tag — sab set."""
    has_src = bool(str(cfg.get("source_channel") or "").strip())
    has_dst = bool(str(cfg.get("channel") or "").strip())
    tag_ok = bool((cfg.get("tag") or "").strip()) or not cfg.get("allow_amazon", True)
    return has_src and has_dst and tag_ok


def needs_setup(uid: int) -> bool:
    """Setup button dikhana hai? Koi task poora set ho ya kabhi post ho chuki ho → nahi."""
    if is_admin(uid):
        return False
    try:
        if any(task_complete(t["cfg"]) for t in list_tasks(uid)):
            return False
        if user_stats(uid, day_start_naive()).get("total", 0) > 0:
            return False            # forwarding chal chuki hai
    except Exception as e:
        logger.error(f"needs_setup error ({uid}): {e}")
        return False
    return True


def wizard_task(uid: int):
    """Wizard kis task pe chale — default / pehla task; koi na ho to naya banao."""
    from task_ui import default_task
    t = default_task(uid)
    if t:
        return t
    tasks = list_tasks(uid)
    if tasks:
        return tasks[0]
    tid = create_task(uid, new_task_config("Task 1"))
    if tid:
        set_default_task(uid, tid)
        return get_task(tid, uid)
    return None


def first_open_step(cfg: dict) -> int:
    if not str(cfg.get("source_channel") or "").strip():
        return 0
    if not str(cfg.get("channel") or "").strip():
        return 1
    if not (cfg.get("tag") or "").strip() and cfg.get("allow_amazon", True):
        return 2
    return 3


# =============================================================================
# SCREENS
# =============================================================================
def _progress(cfg: dict, step: int) -> str:
    done = [bool(str(cfg.get("source_channel") or "").strip()),
            bool(str(cfg.get("channel") or "").strip()),
            bool((cfg.get("tag") or "").strip())]
    marks = "".join("✅" if d else "⬜" for d in done) + ("✅" if step == 3 and all(done) else "⬜")
    return f"🧭 <b>Setup — Step {step + 1}/4</b>  {marks}\n\n"


def _nav(step: int, lang: str) -> list:
    row = []
    if step > 0:
        row.append(btn(tr(lang, "⬅️ Back", "⬅️ Pichla"), callback_data=f"wz:go:{step - 1}"))
    if step < 3:
        row.append(btn(tr(lang, "⏭️ Later", "⏭️ Baad mein"), callback_data=f"wz:go:{step + 1}"))
    return row


def step_screen(uid: int, task: dict, step: int):
    lang = get_lang(uid)
    c = task["cfg"]
    head = _progress(c, step)
    close = [btn(tr(lang, "❌ Close setup", "❌ Setup band karein"), callback_data="wz:close")]

    if step == 0:
        cur = chan(c.get("source_title"), c.get("source_username"), tr(lang, "not set", "set nahi"))
        text = head + tr(lang,
            "📥 <b>Draft Channel</b>\n\n"
            "ℹ️ <b>What is it?</b> The channel where <b>you post raw deals</b>. The bot picks every deal "
            "from here and posts it to your <b>Destination</b> channel — with your tag and design.\n\n"
            "❗ <b>Required:</b> Yes — otherwise you'll have to send every deal to the bot in DM.\n\n"
            "💡 <b>Tip:</b> Make a <b>new private channel</b> just for drafts (only you inside). "
            "Then make this bot an <b>admin</b> there — only then can the bot read your deals.\n\n"
            f"Current: {cur}",
            "📥 <b>Draft Channel</b>\n\n"
            "ℹ️ <b>Ye kya hai?</b> Wo channel jahan <b>aap raw deal / post daaloge</b>. Bot yahan se har deal "
            "uthakar aapke <b>Destination</b> channel mein post karega — aapke tag aur design ke saath.\n\n"
            "❗ <b>Zaroori:</b> Haan — warna har deal bot ko DM mein bhejni padegi.\n\n"
            "💡 <b>Sujhav:</b> Draft ke liye ek <b>naya private channel</b> bana lo (sirf aap usme). "
            "Phir is bot ko us channel mein <b>admin</b> banao — tabhi bot aapki deal padh payega.\n\n"
            f"Abhi: {cur}")
        kb = [[btn(tr(lang, "📥 Choose Draft Channel", "📥 Draft Channel chunein"), GREEN,
                   callback_data="wz:set:src")], _nav(step, lang), close]

    elif step == 1:
        cur = chan(c.get("channel_title"), c.get("channel_username"), tr(lang, "not set", "set nahi"))
        text = head + tr(lang,
            "📢 <b>Destination Channel</b>\n\n"
            "ℹ️ <b>What is it?</b> The channel where you want your <b>deals to be posted</b> — your main "
            "channel that people follow.\n\n"
            "❗ <b>Required:</b> Yes — without it the bot has nowhere to post.\n\n"
            "💡 Make this bot an <b>admin</b> there with <b>Post Messages</b> permission.\n\n"
            f"Current: {cur}",
            "📢 <b>Destination Channel</b>\n\n"
            "ℹ️ <b>Ye kya hai?</b> Wo channel jahan aap <b>deals post karna chahte ho</b> — aapka asli "
            "channel jise log follow karte hain.\n\n"
            "❗ <b>Zaroori:</b> Haan — iske bina bot kahin post nahi kar sakta.\n\n"
            "💡 Is bot ko wahan <b>admin</b> banao, <b>Post Messages</b> permission ke saath.\n\n"
            f"Abhi: {cur}")
        kb = [[btn(tr(lang, "📢 Choose Destination", "📢 Destination chunein"), GREEN,
                   callback_data="wz:set:dest")], _nav(step, lang), close]

    elif step == 2:
        cur = c.get("tag") or tr(lang, "not set", "set nahi")
        text = head + tr(lang,
            "🏷️ <b>Amazon Affiliate Tag</b>\n\n"
            "ℹ️ <b>What is it?</b> Your Amazon Associates tag (like <code>mydeals-21</code>). The bot adds it "
            "to every Amazon link, so the <b>commission comes to you</b>.\n\n"
            "❗ <b>Required:</b> Yes for Amazon deals — <b>without it your commission is missed</b>, so Amazon "
            "deals are not posted until it's set.\n\n"
            "📍 Find it in Amazon Associates (affiliate-program.amazon.in), top-right corner.\n\n"
            f"Current: <code>{esc(cur)}</code>",
            "🏷️ <b>Amazon Affiliate Tag</b>\n\n"
            "ℹ️ <b>Ye kya hai?</b> Aapka Amazon Associates tag (jaise <code>mydeals-21</code>). Bot ise har "
            "Amazon link mein lagata hai, taaki <b>commission aapko mile</b>.\n\n"
            "❗ <b>Zaroori:</b> Haan, Amazon deals ke liye — <b>iske bina aapka commission miss ho jayega</b>, "
            "isliye tag set hone tak Amazon deals post nahi hongi.\n\n"
            "📍 Amazon Associates (affiliate-program.amazon.in) mein upar right corner pe milta hai.\n\n"
            f"Abhi: <code>{esc(cur)}</code>")
        kb = [[btn(tr(lang, "🏷️ Set Affiliate Tag", "🏷️ Affiliate Tag set karein"), GREEN,
                   callback_data="wz:set:tag")], _nav(step, lang), close]

    else:
        missing = []
        if not str(c.get("source_channel") or "").strip():
            missing.append((0, tr(lang, "📥 Draft channel", "📥 Draft channel")))
        if not str(c.get("channel") or "").strip():
            missing.append((1, tr(lang, "📢 Destination channel", "📢 Destination channel")))
        if not (c.get("tag") or "").strip() and c.get("allow_amazon", True):
            missing.append((2, "🏷️ Amazon Affiliate Tag"))
        if missing:
            text = head + tr(lang,
                "⚠️ <b>Setup is not complete yet</b>\n\nStill to do:\n"
                + "\n".join(f"• {name}" for _, name in missing)
                + "\n\nNo sample post is sent until these are set. Tap below to finish 👇",
                "⚠️ <b>Setup abhi poora nahi hua</b>\n\nYe baaki hai:\n"
                + "\n".join(f"• {name}" for _, name in missing)
                + "\n\nJab tak ye set nahi, sample post nahi jayegi. Neeche se poora karein 👇")
            kb = [[btn(f"🔧 {name}", callback_data=f"wz:go:{i}")] for i, name in missing]
            kb += [[btn("🏠 Home", callback_data="home")]]
        else:
            where = chan(c.get("channel_title"), c.get("channel_username"))
            text = head + tr(lang,
                "🎉 <b>All set!</b>\n\n"
                f"✅ Draft: {chan(c.get('source_title'), c.get('source_username'))}\n"
                f"✅ Destination: {where}\n"
                f"✅ Amazon Affiliate Tag: <code>{esc(c.get('tag') or '—')}</code>\n\n"
                "🧪 <b>Sample post:</b> tap below and a sample Amazon deal goes to your Destination with "
                "your tag — see how your posts will look.\n\n"
                "🎨 You can customise how posts look (card, watermark, buttons…) in ⚙️ Settings.\n\n"
                "👉 After this, just post deals in your Draft channel — the bot does the rest.",
                "🎉 <b>Sab ready hai!</b>\n\n"
                f"✅ Draft: {chan(c.get('source_title'), c.get('source_username'))}\n"
                f"✅ Destination: {where}\n"
                f"✅ Amazon Affiliate Tag: <code>{esc(c.get('tag') or '—')}</code>\n\n"
                "🧪 <b>Sample post:</b> neeche dabao — ek sample Amazon deal aapke tag ke saath Destination "
                "mein jayegi, dekh lo post kaisi dikhegi.\n\n"
                "🎨 Post kaisi dikhe (card, watermark, buttons…) ye ⚙️ Settings mein apne hisaab se badal "
                "sakte ho.\n\n"
                "👉 Iske baad bas Draft channel mein deal daalo — baaki bot kar dega.")
            kb = [[btn(tr(lang, "🧪 Send sample post", "🧪 Sample post bhejo"), GREEN, callback_data="wz:sample")],
                  [btn(tr(lang, "⚙️ Settings", "⚙️ Settings"), BLUE, callback_data=f"t:{task['id']}"),
                   btn("🏠 Home", callback_data="home")]]
        kb = [r for r in kb if r]
        return text, InlineKeyboardMarkup(kb)

    return text, InlineKeyboardMarkup([r for r in kb if r])


def settings_picker(uid: int):
    """⚙️ Settings — task chuno, uski settings screen (task screen) khule."""
    from task_ui import tname
    lang = get_lang(uid)
    tasks = list_tasks(uid)
    if not tasks:
        return (tr(lang, "⚙️ You have no task yet. Create one first.",
                   "⚙️ Abhi koi task nahi hai. Pehle task banayein."),
                InlineKeyboardMarkup([[btn(tr(lang, "➕ New Task", "➕ Naya Task"), GREEN, callback_data="tn")]]))
    rows = [[btn(f"{'⏸️' if t['paused'] else '▶️'} {tname(t, lang)}", callback_data=f"t:{t['id']}")]
            for t in tasks[:20]]
    rows.append([btn("🏠 Home", callback_data="home")])
    return (tr(lang, "⚙️ <b>Settings</b>\n\nWhich task's settings do you want to change?",
               "⚙️ <b>Settings</b>\n\nKis task ki settings badalni hain? Chunein 👇"),
            InlineKeyboardMarkup(rows))


# =============================================================================
# CALLBACKS
# =============================================================================
async def handle(query, context, uid: int, data: str):
    """True = sambhal liya; "home" = Home dikhao; False = wizard ka nahi."""
    if not data.startswith("wz:"):
        return False
    from task_ui import show, _ask, chan_text, tag_ask_text, show_picker
    lang = get_lang(uid)
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else "go"
    task = wizard_task(uid)
    if not task:
        await query.answer(tr(lang, "Could not create a task.", "Task nahi ban paaya."), show_alert=True)
        return True
    tid = task["id"]

    if act == "close":
        for k in ("wz_tid", "action", "tid"):
            context.user_data.pop(k, None)
        return "home"                       # main.py Home dikhata hai

    if act == "go":
        try:
            step = int(parts[2])
        except (IndexError, ValueError):
            step = first_open_step(task["cfg"])
        step = max(0, min(3, step))
        context.user_data.pop("action", None)
        context.user_data["wz_tid"] = tid
        text, kb = step_screen(uid, task, step)
        await show(query, context, text, kb)
        return True

    if act == "set" and len(parts) > 2:
        what = parts[2]
        context.user_data["wz_tid"] = tid
        if what in ("src", "dest"):
            _ask(context, "t_src" if what == "src" else "t_dest", tid)
            back = 0 if what == "src" else 1
            await show(query, context, chan_text(task, what, lang),
                       InlineKeyboardMarkup([[btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"wz:go:{back}")]]))
            await show_picker(context, query.message, what, lang)
            return True
        if what == "tag":
            _ask(context, "t_tag", tid)
            await show(query, context, tag_ask_text(task, lang),
                       InlineKeyboardMarkup([[btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data="wz:go:2")]]))
            return True
        return True

    if act == "sample":
        await query.answer(tr(lang, "🧪 Sending sample post…", "🧪 Sample post bhej raha hoon…"))
        ok, detail = await send_sample(context, uid, task, lang)
        c = task["cfg"]
        where = chan(c.get("channel_title"), c.get("channel_username"))
        if ok:
            text = tr(lang,
                      f"✅ <b>Sample post sent to {where}!</b>\n\nOpen the channel and see how it looks.\n\n"
                      "🎨 Want it different? Change card, watermark, buttons and more in ⚙️ Settings.\n"
                      "👉 Now just post deals in your Draft channel — the bot will do the rest.",
                      f"✅ <b>Sample post {where} mein chali gayi!</b>\n\nChannel kholke dekh lo kaisi dikhti hai.\n\n"
                      "🎨 Alag chahiye? Card, watermark, buttons waghera ⚙️ Settings mein badlo.\n"
                      "👉 Ab bas Draft channel mein deal daalo — baaki bot kar dega.")
        else:
            text = tr(lang, f"❌ <b>Sample post failed</b>\n{esc(detail)}\n\nFix it and try again.",
                      f"❌ <b>Sample post nahi gayi</b>\n{esc(detail)}\n\nIse theek karke dobara try karein.")
        context.user_data.pop("wz_tid", None)
        await show(query, context, text, InlineKeyboardMarkup([
            [btn(tr(lang, "🔁 Try again", "🔁 Dobara bhejo"), callback_data="wz:sample")] if not ok else [],
            [btn("⚙️ Settings", BLUE, callback_data=f"t:{tid}"), btn("🏠 Home", callback_data="home")],
        ]))
        return True
    return True


async def send_sample(context, uid: int, task: dict, lang: str):
    """Destination mein ek sample Amazon deal, user ke tag ke saath. (ok, detail)"""
    from amazon_api import get_product_by_asin
    from engine import post_amazon_product, deliver
    c = task["cfg"]
    sample_task = dict(task, cfg=dict(c, dup_check=False))      # sample baar-baar bhej sakein
    try:
        prod = await get_product_by_asin(SAMPLE_ASIN)
    except Exception:
        prod = None
    if prod and prod.get("title"):
        status, detail, _ = await post_amazon_product(context, uid, sample_task, prod, lang)
        return status == "posted", detail
    # Amazon API se data nahi mila — simple text sample
    link = make_affiliate_url(SAMPLE_ASIN, c.get("tag") or "")
    text = tr(lang, f"🧪 <b>Sample post</b>\n\n🔥 Example Amazon deal\n🛒 {link}",
              f"🧪 <b>Sample post</b>\n\n🔥 Example Amazon deal\n🛒 {link}")
    try:
        await deliver(context.bot.send_message, dict(chat_id=str(c.get("channel")), text=text,
                                                     parse_mode=ParseMode.HTML, disable_web_page_preview=True))
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


async def continue_after_setting(context, reply, uid: int, tid: int) -> bool:
    """Wizard ke beech koi setting set hui — task screen ki jagah agla step dikhao.
    reply(text, kb) — naya message bhejne wala function. True = wizard ne sambhal liya."""
    if context.user_data.get("wz_tid") != tid:
        return False
    task = get_task(tid, uid)
    if not task:
        return False
    text, kb = step_screen(uid, task, first_open_step(task["cfg"]))
    await reply(text, kb)
    return True

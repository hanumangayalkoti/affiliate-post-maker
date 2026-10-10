"""
task_ui.py — TASKS. Har task = 1 Draft channel + 1 Destination channel +
apni saari settings (tag, filter, duplicate, details, buttons, header/footer,
watermark, Image Card). Plan (tier) ke hisaab se kitne tasks chal sakte hain.

Callback format (sab 64 byte ke andar):
  tl                     tasks list
  tn                     naya task
  t:<id>                 task home
  t:<id>:<action>[:...]  task ki setting
  ch:<d|s>:<chat>:<id|new>  bot ko channel mein admin banaya — kahan use karein
"""
import re
import html as html_lib
import logging

from telegram import (
    InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton,
    KeyboardButtonRequestChat, ChatAdministratorRights,
)
from telegram.constants import ParseMode

from alerts import notify_task_event
from amazon_api import is_valid_tag
from caption import FIELD_LABELS, FIELD_ORDER
from card import WM_POSITIONS, WM_SIZES, WM_COLORS, clean_watermark
from storage import (
    list_tasks, get_task, create_task, save_task, set_task_paused, delete_task,
    new_task_config,
)
from ui import btn, tr, chan, style_name, next_style, track, GREEN, BLUE, RED
from users import get_user, get_lang, limits, set_default_task, is_active

logger = logging.getLogger(__name__)
esc = html_lib.escape

# Jin sawaalon ka jawab link hi hai — user link bheje to unhe cancel nahi karte
LINK_ACTIONS = {"t_dest", "t_src", "t_btn_link"}

MAX_TASK_NAME = 24


# =============================================================================
# HELPERS
# =============================================================================
def tname(task: dict, lang: str = "hi") -> str:
    name = (task.get("cfg", {}).get("name") or "").strip()
    return name or f"Task #{task['id']}"


def running_ids(uid: int) -> list:
    """Jo tasks sach mein chal sakte hain: unpaused, aur plan ki limit tak (purane pehle)."""
    lim = limits(uid)["tasks"]
    ids = [t["id"] for t in list_tasks(uid) if not t["paused"]]
    return ids[:lim]


def default_task(uid: int):
    """DM wale messages is task se jaate hain. Set na ho to pehla chalta task."""
    u = get_user(uid) or {}
    run = running_ids(uid)
    tasks = {t["id"]: t for t in list_tasks(uid)}
    d = u.get("default_task")
    if d in tasks and d in run:
        return tasks[d]
    for tid in run:
        return tasks[tid]
    return None


def _same(a, b) -> bool:
    return bool(str(a or "").strip()) and str(a).strip() == str(b or "").strip()


def task_notes(uid: int, task: dict, lang: str) -> list:
    """Same channel doosre task mein ho / chain ho to user ko bata do (rokte nahi)."""
    notes = []
    cfg = task["cfg"]
    for other in list_tasks(uid):
        if other["id"] == task["id"]:
            continue
        o, on = other["cfg"], esc(tname(other, lang))
        if _same(cfg.get("source_channel"), o.get("source_channel")):
            notes.append(tr(lang, f"ℹ️ The same Draft is used in <b>{on}</b> — each deal will post in both tasks.",
                            f"ℹ️ Yahi Draft <b>{on}</b> mein bhi hai — har deal dono tasks se post hogi."))
        if _same(cfg.get("channel"), o.get("channel")):
            notes.append(tr(lang, f"ℹ️ The same Destination is used in <b>{on}</b>.",
                            f"ℹ️ Yahi Destination <b>{on}</b> mein bhi hai."))
        if _same(cfg.get("channel"), o.get("source_channel")):
            notes.append(tr(lang,
                            f"ℹ️ This Destination is the Draft of <b>{on}</b>. Bots can't read their own posts, "
                            f"so these posts will <b>not</b> go further to {on}. (No loop, no extra messages used.)",
                            f"ℹ️ Ye Destination <b>{on}</b> ka Draft hai. Bot apni khud ki post nahi padhta, "
                            f"isliye ye posts aage {on} mein <b>nahi</b> jayengi. (Loop nahi banega, "
                            f"message limit nahi kategi.)"))
        if _same(cfg.get("source_channel"), o.get("channel")):
            notes.append(tr(lang,
                            f"ℹ️ This Draft is the Destination of <b>{on}</b>. Posts made by the bot there "
                            f"will <b>not</b> be picked up here — only posts you add yourself.",
                            f"ℹ️ Ye Draft <b>{on}</b> ka Destination hai. Wahan bot ki daali posts yahan "
                            f"<b>nahi</b> uthayi jayengi — sirf aapki khud ki daali posts."))
    return notes


async def show(query, context, text: str, kb=None):
    """Same message edit karo; na ho paaye (photo wagaira) to naya bhejo."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                      disable_web_page_preview=True)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        m = await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                           disable_web_page_preview=True)
        track(context, m)


def _onoff(v) -> str:
    return "✅" if v else "❌"


def back_row(tid: int, lang: str):
    return [btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"t:{tid}"),
            btn(tr(lang, "📋 All Tasks", "📋 Saare Tasks"), callback_data="tl")]


# =============================================================================
# CHANNEL VERIFY
# =============================================================================
def channel_from_message(msg):
    """Forward kiye message se channel ID (int) ya None."""
    fc = getattr(msg, "forward_from_chat", None)
    if fc is not None and getattr(fc, "type", "") == "channel":
        return fc.id
    origin = getattr(msg, "forward_origin", None)
    chat = getattr(origin, "chat", None) if origin is not None else None
    if chat is not None and getattr(chat, "type", "") == "channel":
        return chat.id
    raw = (getattr(msg, "api_kwargs", None) or {})
    o = raw.get("forward_origin") or {}
    c = o.get("chat") or raw.get("forward_from_chat") or {}
    if isinstance(c, dict) and c.get("type") == "channel" and c.get("id"):
        return int(c["id"])
    return None


_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z0-9_]{4,})/?(?:\d+)?$", re.I)
_BTN_URL_RE = re.compile(r"^(https?://[^\s/$.?#][^\s]*\.[^\s]{2,}|tg://[^\s]+)$", re.I)


def normalize_channel_ident(text: str):
    """@name / name / t.me/name / -100… → get_chat ke layak."""
    t = (text or "").strip()
    if not t:
        return None
    if t.lstrip("-").isdigit():
        return t
    m = _TME_RE.match(t)
    if m:
        return "@" + m.group(1)
    if "t.me/+" in t or "joinchat" in t:
        return None
    t = t.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", t):
        return "@" + t
    return None


async def verify_channel(bot, ident, uid: int, need_post: bool, lang: str):
    """Returns (chat, error_text_or_None). Bot admin ho, user bhi admin ho."""
    try:
        chat = await bot.get_chat(ident)
    except Exception:
        return None, tr(lang, "Channel not found. First make the bot an admin in the channel, "
                              "then send its @username or forward a post from it.",
                        "Channel nahi mila. Pehle bot ko channel mein admin banayein, "
                        "phir @username bhejein ya channel ka koi post forward karein.")
    if chat.type != "channel":
        return None, tr(lang, "This is not a channel. Only Telegram channels work.",
                        "Ye channel nahi hai. Sirf Telegram channel chalega.")
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        ok = me.status == "administrator"
    except Exception:
        ok = False
    if not ok:
        return None, tr(lang, "The bot is not an admin in this channel. Make it an admin first.",
                        "Bot is channel mein admin nahi hai. Pehle admin banayein.")
    if need_post and not getattr(me, "can_post_messages", False):
        return None, tr(lang, "The bot is an admin but 'Post Messages' is OFF — please turn it ON.",
                        "Bot admin hai par 'Post Messages' permission band hai — ON karein.")
    try:
        mem = await bot.get_chat_member(chat.id, uid)
        if mem.status not in ("creator", "administrator"):
            raise ValueError
    except Exception:
        return None, tr(lang, "You are not an admin of this channel — you can only add your own channel.",
                        "Aap is channel ke admin nahi hain — sirf apna channel jod sakte hain.")
    return chat, None


# Telegram ka apna "channel chunein" button — private channel bhi, bina forward / ID ke.
PICK_DEST, PICK_SRC = 1, 2


def _rights(post: bool) -> ChatAdministratorRights:
    return ChatAdministratorRights(False, False, False, False, False, False, False, False,
                                   can_post_messages=post)


def picker_kb(kind: str, lang: str) -> ReplyKeyboardMarkup:
    label = (tr(lang, "📢 Choose Channel", "📢 Channel chunein") if kind == "dest"
             else tr(lang, "📥 Choose Channel", "📥 Channel chunein"))
    req = KeyboardButtonRequestChat(
        request_id=PICK_DEST if kind == "dest" else PICK_SRC,
        chat_is_channel=True,
        user_administrator_rights=_rights(True),     # user khud admin ho (apna hi channel)
        bot_administrator_rights=_rights(True),      # bot admin na ho to Telegram add karwata hai
    )
    return ReplyKeyboardMarkup([[KeyboardButton(label, request_chat=req)]], resize_keyboard=True,
                               one_time_keyboard=True)


async def show_picker(context, message, kind: str, lang: str):
    m = await message.reply_text(
        tr(lang, "👇 Tap the button below and pick your channel.", "👇 Neeche button dabake apna channel chunein."),
        reply_markup=picker_kb(kind, lang))
    context.user_data["picker_on"] = True
    track(context, m)


async def drop_picker(context, bot, chat_id: int):
    """Neeche wala 'Channel chunein' keyboard hatao (agar dikh raha ho)."""
    if not context.user_data.pop("picker_on", False):
        return
    try:
        m = await bot.send_message(chat_id, "⌨️", reply_markup=ReplyKeyboardRemove())
        await m.delete()
    except Exception:
        pass


async def handle_chat_shared(update: Update, context):
    """User ne 'Channel chunein' se channel chuna."""
    msg = update.message
    shared = getattr(msg, "chat_shared", None)
    if not shared or not update.effective_user:
        return
    uid = update.effective_user.id
    lang = get_lang(uid)
    action = context.user_data.get("action")
    tid = context.user_data.get("tid")
    kind = "dest" if shared.request_id == PICK_DEST else "src"
    context.user_data.pop("picker_on", None)
    track(context, msg)
    if action not in ("t_dest", "t_src") or not tid or not get_task(tid, uid):
        m = await msg.reply_text(tr(lang, "⚠️ Open the task in /tasks first, then choose the channel.",
                                    "⚠️ Pehle /tasks mein task kholein, phir channel chunein."),
                                 reply_markup=ReplyKeyboardRemove())
        track(context, m)
        return
    ok, out = await set_task_channel(context.bot, uid, tid, shared.chat_id, kind, lang)
    if ok:
        context.user_data.pop("action", None)
        await msg.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())  # report
        task = get_task(tid, uid)
        m = await msg.reply_text(task_text(uid, task, lang), parse_mode=ParseMode.HTML,
                                 reply_markup=task_kb(uid, task, lang), disable_web_page_preview=True)
        track(context, m)
    else:
        m = await msg.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=picker_kb(kind, lang))
        context.user_data["picker_on"] = True
        track(context, m)


async def set_task_channel(bot, uid: int, tid: int, ident, kind: str, lang: str):
    """kind 'dest' / 'src'. Returns (ok, text)."""
    task = get_task(tid, uid)
    if not task:
        return False, tr(lang, "❌ Task not found.", "❌ Task nahi mila.")
    chat, err = await verify_channel(bot, ident, uid, need_post=(kind == "dest"), lang=lang)
    if err:
        return False, "❌ " + err
    cfg = task["cfg"]
    cid = str(chat.id)
    if kind == "dest":
        if _same(cid, cfg.get("source_channel")):
            return False, tr(lang, "⚠️ This is the Draft of this task. Destination must be a different channel.",
                             "⚠️ Ye isi task ka Draft hai. Destination alag channel hona chahiye.")
        cfg.update(channel=cid, channel_title=chat.title or cid, channel_username=chat.username or "")
    else:
        if _same(cid, cfg.get("channel")):
            return False, tr(lang, "⚠️ This is the Destination of this task. Draft must be a different channel.",
                             "⚠️ Ye isi task ka Destination hai. Draft alag channel hona chahiye.")
        # Doosre account ka bhi Draft ho sakta hai — dono us channel ke admin hain
        # (verify_channel ne check kiya), aur har account ke tasks alag chalte hain.
        cfg.update(source_channel=cid, source_title=chat.title or cid, source_username=chat.username or "")
    if not save_task(uid, tid, cfg):
        return False, tr(lang, "❌ Could not save, please try again.", "❌ Save nahi hua, dobara try karein.")
    await notify_task_event(bot, uid, get_task(tid, uid),
                            "📢 <b>Destination set hua</b>" if kind == "dest" else "📥 <b>Draft set hua</b>")
    shown = chan(chat.title, chat.username)
    if kind == "dest":
        return True, tr(lang, f"✅ <b>Destination set:</b> {shown}\nDeals will be posted here.",
                        f"✅ <b>Destination set:</b> {shown}\nDeals yahan post hongi.")
    return True, tr(lang, f"✅ <b>Draft set:</b> {shown}\nPost a deal here — the bot will pick it up.",
                    f"✅ <b>Draft set:</b> {shown}\nIsme deal daalein — bot khud utha lega.")


# =============================================================================
# TASK LIMITS
# =============================================================================
def can_add_task(uid: int) -> bool:
    return len(list_tasks(uid)) < limits(uid)["tasks"]


def enforce_task_limit(uid: int) -> list:
    """Plan ki limit se zyada tasks chal rahe hon to naye wale pause. Paused ids wapas."""
    lim = limits(uid)["tasks"]
    if lim <= 0:
        return []          # plan hi nahi — posting waise bhi band, tasks ko mat chhedo
    active = [t for t in list_tasks(uid) if not t["paused"]]
    paused = []
    for t in active[lim:]:
        if set_task_paused(uid, t["id"], True):
            paused.append(t["id"])
    return paused


# =============================================================================
# SCREENS
# =============================================================================
def tasks_text(uid: int, lang: str) -> str:
    tasks = list_tasks(uid)
    lim = limits(uid)
    run = running_ids(uid)
    d = default_task(uid)
    head = tr(lang, f"📋 <b>Your Tasks</b> ({len(tasks)}/{lim['tasks']})\n\n",
              f"📋 <b>Aapke Tasks</b> ({len(tasks)}/{lim['tasks']})\n\n")
    info = tr(lang,
              "A <b>Task</b> = 1 Draft channel ➜ 1 Destination channel, with its own settings.\n"
              "⭐ = Default task — deals you send to the bot in DM go through it.",
              "<b>Task</b> = 1 Draft channel ➜ 1 Destination channel, apni settings ke saath.\n"
              "⭐ = Default task — bot ko DM mein bheji deals isi se jaati hain.")
    if not tasks:
        return head + info + tr(lang, "\n\nNo task yet. Tap <b>➕ New Task</b>.",
                                "\n\nAbhi koi task nahi. <b>➕ Naya Task</b> dabayein.")
    lines = [head + info + "\n"]
    for t in tasks:
        c = t["cfg"]
        state = "▶️" if t["id"] in run else "⏸️"
        star = " ⭐" if d and d["id"] == t["id"] else ""
        src = chan(c.get("source_title"), c.get("source_username"), tr(lang, "DM only", "sirf DM"))
        dst = chan(c.get("channel_title"), c.get("channel_username"), "❌")
        lines.append(f"{state} <b>{esc(tname(t, lang))}</b>{star}\n     📥 {src} ➜ 📢 {dst}")
    if lim["tasks"] and len(tasks) >= lim["tasks"]:
        lines.append(tr(lang, f"\n<i>Your {lim['name']} plan allows {lim['tasks']} task(s). Need more? /plan</i>",
                        f"\n<i>Aapke {lim['name']} plan mein {lim['tasks']} task. Zyada chahiye? /plan</i>"))
    return "\n".join(lines)


def tasks_kb(uid: int, lang: str) -> InlineKeyboardMarkup:
    run = running_ids(uid)
    rows = []
    for t in list_tasks(uid):
        state = "▶️" if t["id"] in run else "⏸️"
        rows.append([btn(f"{state} {tname(t, lang)}", callback_data=f"t:{t['id']}")])
    rows.append([btn(tr(lang, "➕ New Task", "➕ Naya Task"), GREEN, callback_data="tn")])
    rows.append([btn(tr(lang, "🏠 Home", "🏠 Home"), callback_data="home")])
    return InlineKeyboardMarkup(rows)


def task_text(uid: int, task: dict, lang: str) -> str:
    c = task["cfg"]
    run = task["id"] in running_ids(uid)
    d = default_task(uid)
    lim = limits(uid)
    state = (tr(lang, "▶️ Running", "▶️ Chal raha hai") if run
             else tr(lang, "⏸️ Paused", "⏸️ Ruka hua"))
    star = tr(lang, "  ⭐ Default", "  ⭐ Default") if d and d["id"] == task["id"] else ""
    posts = []
    if c.get("allow_amazon", True):
        posts.append("🛍️ Amazon")
    if c.get("allow_other", True):
        posts.append("📝 Non-Amazon")
    card = c.get("card", {})
    card_line = ("🔒 Pro" if not lim["card"] else
                 (tr(lang, "✅ ON", "✅ ON") if card.get("enabled") else "❌ OFF"))
    wm = c.get("watermark", {})
    lines = [
        f"📋 <b>{esc(tname(task, lang))}</b> — {state}{star}\n",
        f"📥 Draft: {chan(c.get('source_title'), c.get('source_username'), tr(lang, '<i>not set (DM works)</i>', '<i>set nahi (DM se chalega)</i>'))}",
        f"📢 Destination: {chan(c.get('channel_title'), c.get('channel_username'), '❌ ' + tr(lang, 'not set', 'set nahi'))}",
        f"🏷️ {tr(lang, 'Affiliate Tag', 'Affiliate Tag')}: <code>{esc(c.get('tag') or '—')}</code>\n",
        f"🔍 {tr(lang, 'Posts', 'Posts')}: {' + '.join(posts)}",
        f"♻️ {tr(lang, 'Duplicate check', 'Duplicate check')}: {_onoff(c.get('dup_check', True))}",
        f"🚫 Remove t.me link &amp; Username: {_onoff(c.get('strip_promo', True))}",
        f"🅱️ Bold Link: {_onoff(c.get('bold_links', True))}",
        f"📉 Discount Filter: {disc_label(c, lang)}",
        f"🎨 Image Card: {card_line}",
        f"💧 Watermark: {_onoff(wm.get('enabled') and wm.get('text'))}",
        f"🛒 Amazon Logo: {_onoff(c.get('amazon_badge', True))}",
    ]
    notes = task_notes(uid, task, lang)
    if not run and not task["paused"]:
        notes.insert(0, tr(lang, f"⚠️ Your plan allows {lim['tasks']} running task(s) — this one is not running.",
                           f"⚠️ Aapke plan mein {lim['tasks']} task chal sakte hain — ye abhi nahi chal raha."))
    if notes:
        lines.append("\n" + "\n".join(notes))
    lines.append(tr(lang, "\n<i>Tap any setting below to change it.</i>",
                    "\n<i>Jo badalna hai uspe tap karein.</i>"))
    return "\n".join(lines)


def task_kb(uid: int, task: dict, lang: str) -> InlineKeyboardMarkup:
    tid = task["id"]
    c = task["cfg"]
    lim = limits(uid)
    d = default_task(uid)
    card_label = "🎨 Image Card" + ("" if lim["card"] else " 🔒")
    rows = [
        [btn(f"🏷️ Tag: {c.get('tag') or tr(lang, 'set it', 'set karein')} ✏️", callback_data=f"t:{tid}:tag"),
         btn("📢 Destination", callback_data=f"t:{tid}:dest")],
        [btn("📥 Draft", callback_data=f"t:{tid}:src"),
         btn(tr(lang, "🔍 Which Posts", "🔍 Kaun Si Posts"), callback_data=f"t:{tid}:filt")],
        [btn(card_label, callback_data=f"t:{tid}:card"),
         btn("💧 Watermark", callback_data=f"t:{tid}:wm")],
        [btn(tr(lang, "🛍️ Post Details", "🛍️ Post Details"), callback_data=f"t:{tid}:amz"),
         btn(tr(lang, "🎛️ Buttons", "🎛️ Buttons"), callback_data=f"t:{tid}:btns")],
        [btn("🔝 Header", callback_data=f"t:{tid}:hf:header"),
         btn("🔚 Footer", callback_data=f"t:{tid}:hf:footer")],
        [btn(f"♻️ Duplicate {_onoff(c.get('dup_check', True))}", callback_data=f"t:{tid}:dup"),
         btn(f"🔔 {tr(lang, 'Notification', 'Notification')}", callback_data=f"t:{tid}:silent")],
        [btn(f"📉 Discount Filter — {disc_label(c, lang)}", callback_data=f"t:{tid}:disc")],
    ]
    # Pause / Resume chhota — Search Links ke bagal mein (2×2 jaisa)
    if task["paused"]:
        pause_btn = btn(tr(lang, "▶️ Resume", "▶️ Chalu karein"), GREEN, callback_data=f"t:{tid}:resume")
    else:
        pause_btn = btn(tr(lang, "⏸️ Pause", "⏸️ Rokein"), callback_data=f"t:{tid}:pause")
    rows.append([btn(f"🔗 Search Links {_onoff(c.get('search_links'))}", callback_data=f"t:{tid}:search"),
                 pause_btn])
    # Lamba naam — poori line, taaki text pura dikhe
    rows.append([btn(f"{_onoff(c.get('strip_promo', True))} 🚫 Remove t.me link and Username",
                     callback_data=f"t:{tid}:promo")])
    rows.append([btn(f"🛒 Amazon Logo {_onoff(c.get('amazon_badge', True))}", callback_data=f"t:{tid}:badge"),
                 btn(f"🅱️ Bold Link {_onoff(c.get('bold_links', True))}", callback_data=f"t:{tid}:bold")])
    if not (d and d["id"] == tid):
        rows.append([btn(tr(lang, "⭐ Make Default", "⭐ Default banayein"), callback_data=f"t:{tid}:def")])
    rows.append([btn(tr(lang, "✏️ Rename", "✏️ Naam badlein"), callback_data=f"t:{tid}:ren"),
                 btn(tr(lang, "🗑️ Delete", "🗑️ Delete"), RED, callback_data=f"t:{tid}:del")])
    rows.append([btn(tr(lang, "📋 All Tasks", "📋 Saare Tasks"), callback_data="tl"),
                 btn("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)


# ── Tag ──────────────────────────────────────────────────────────────────
def tag_text(task, lang):
    c = task["cfg"]
    return tr(lang,
              f"🏷️ <b>Affiliate Tag</b> — {esc(tname(task, lang))}\n\n"
              f"Current: <code>{esc(c.get('tag') or 'not set')}</code>\n\n"
              "Your tag is added to <b>every Amazon link and button</b> of this task, so the "
              "commission comes to you.\n\n"
              "✏️ <b>To change it, just send the new tag now</b> (example: <code>mydeals-21</code>)\n"
              "<i>Find it in Amazon Associates (affiliate-program.amazon.in), top-right corner.</i>",
              f"🏷️ <b>Affiliate Tag</b> — {esc(tname(task, lang))}\n\n"
              f"Abhi: <code>{esc(c.get('tag') or 'set nahi')}</code>\n\n"
              "Ye tag is task ke <b>har Amazon link aur button</b> mein lagega, taaki kamai aapko mile.\n\n"
              "✏️ <b>Badalna hai to abhi naya tag bhej dein</b> (jaise <code>mydeals-21</code>)\n"
              "<i>Amazon Associates (affiliate-program.amazon.in) mein upar right corner pe milta hai.</i>")


# ── Destination / Draft ─────────────────────────────────────────────────
def chan_text(task, kind, lang):
    c = task["cfg"]
    if kind == "dest":
        cur = chan(c.get("channel_title"), c.get("channel_username"), tr(lang, "not set", "set nahi"))
        return tr(lang,
                  f"📢 <b>Destination</b> — {esc(tname(task, lang))}\n\nCurrent: {cur}\n\n"
                  "The channel where this task <b>posts the deals</b>.\n\n"
                  "<b>How to set (easiest):</b>\n👇 Tap <b>📢 Choose Channel</b> at the bottom and pick your "
                  "channel — <b>private channels work too</b>. If the bot isn't an admin yet, Telegram will "
                  "ask you to add it.\n\n"
                  "<i>Or send the channel's @username, or forward a post from it. "
                  "(Invite links like t.me/+… can't be used.)</i>",
                  f"📢 <b>Destination</b> — {esc(tname(task, lang))}\n\nAbhi: {cur}\n\n"
                  "Wo channel jahan ye task <b>deals post karega</b>.\n\n"
                  "<b>Kaise set karein (sabse aasaan):</b>\n👇 Neeche <b>📢 Channel chunein</b> dabayein aur apna "
                  "channel chunein — <b>private channel bhi chalega</b>. Bot admin na ho to Telegram khud "
                  "admin banane ka option dega.\n\n"
                  "<i>Ya channel ka @username bhejein, ya uska koi post forward karein. "
                  "(t.me/+… wale invite link se nahi hota.)</i>")
    cur = chan(c.get("source_title"), c.get("source_username"), tr(lang, "not set", "set nahi"))
    return tr(lang,
              f"📥 <b>Draft</b> — {esc(tname(task, lang))}\n\nCurrent: {cur}\n\n"
              "A channel where <b>you put deals</b> — the bot picks them up and posts them to the "
              "Destination with your tag. (Optional — you can also just send deals to the bot in DM.)\n\n"
              "<b>How to set (easiest):</b> 👇 tap <b>📥 Choose Channel</b> at the bottom and pick it — "
              "<b>private channels work too</b>.\n<i>Or send its @username, or forward a post from it.</i>",
              f"📥 <b>Draft</b> — {esc(tname(task, lang))}\n\nAbhi: {cur}\n\n"
              "Wo channel jisme <b>aap deals daalte hain</b> — bot wahan se utha ke aapke tag ke saath "
              "Destination pe post karta hai. (Optional — bot ko DM mein bhi deal bhej sakte hain.)\n\n"
              "<b>Kaise set karein (sabse aasaan):</b> 👇 neeche <b>📥 Channel chunein</b> dabayein aur "
              "channel chunein — <b>private channel bhi chalega</b>.\n<i>Ya uska @username bhejein, ya koi "
              "post forward karein.</i>")


def chan_kb(task, kind, lang):
    tid = task["id"]
    rows = []
    if kind == "src" and task["cfg"].get("source_channel"):
        rows.append([btn(tr(lang, "🗑️ Remove Draft", "🗑️ Draft hatayein"), callback_data=f"t:{tid}:src_off")])
    rows.append(back_row(tid, lang))
    return InlineKeyboardMarkup(rows)


# ── Which posts (Amazon / Non-Amazon) ────────────────────────────────────
def filt_text(task, lang):
    c = task["cfg"]
    a, o = c.get("allow_amazon", True), c.get("allow_other", True)
    if a and o:
        now = tr(lang, "✅ <b>All posts</b> go — Amazon and Non-Amazon.",
                 "✅ <b>Saari posts</b> jayengi — Amazon bhi, Non-Amazon bhi.")
    elif a:
        now = tr(lang, "✅ <b>Only Amazon</b> posts go. Non-Amazon posts are skipped.",
                 "✅ <b>Sirf Amazon</b> posts jayengi. Non-Amazon posts skip hongi.")
    else:
        now = tr(lang, "✅ <b>Only Non-Amazon</b> posts go. Amazon posts are skipped.",
                 "✅ <b>Sirf Non-Amazon</b> posts jayengi. Amazon posts skip hongi.")
    return tr(lang,
              f"🔍 <b>Which Posts</b> — {esc(tname(task, lang))}\n\n{now}\n\n"
              "<i>Tap to turn ON/OFF. Green = ON. At least one must stay ON.</i>",
              f"🔍 <b>Kaun Si Posts</b> — {esc(tname(task, lang))}\n\n{now}\n\n"
              "<i>Tap karke ON/OFF karein. Hara = ON. Kam se kam ek ON rehna chahiye.</i>")


def filt_kb(task, lang):
    tid, c = task["id"], task["cfg"]
    a, o = c.get("allow_amazon", True), c.get("allow_other", True)
    return InlineKeyboardMarkup([
        [btn(f"{_onoff(a)} 🛍️ Amazon", GREEN if a else "", callback_data=f"t:{tid}:filt:amz"),
         btn(f"{_onoff(o)} 📝 Non-Amazon", GREEN if o else "", callback_data=f"t:{tid}:filt:oth")],
        back_row(tid, lang),
    ])


# ── Duplicate / Notification / Search links ─────────────────────────────
def dup_text(task, lang):
    on = task["cfg"].get("dup_check", True)
    return tr(lang,
              f"♻️ <b>Duplicate Check</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → the same post is not posted again within 24 hours.\n"
              "• Amazon: same product\n• Other posts: same caption text (or the same photo/video "
              "if there is no text)\n\n<b>OFF</b> → everything is posted, even repeats.",
              f"♻️ <b>Duplicate Check</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → same post 24 ghante mein dobara post nahi hogi.\n"
              "• Amazon: same product\n• Baaki posts: same caption text (text na ho to same "
              "photo/video)\n\n<b>OFF</b> → sab post hoga, repeat bhi.")


def silent_text(task, lang):
    s = task["cfg"].get("silent", True)
    return tr(lang,
              f"🔔 <b>Notification</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'🔕 Silent' if s else '🔔 Loud'}</b>\n\n"
              "<b>Silent</b> → posts arrive without a sound on subscribers' phones.\n"
              "<b>Loud</b> → every post rings a notification.",
              f"🔔 <b>Notification</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'🔕 Silent' if s else '🔔 Loud'}</b>\n\n"
              "<b>Silent</b> → subscribers ke phone pe post bina awaaz aayegi.\n"
              "<b>Loud</b> → har post pe notification bajegi.")


def badge_text(task, lang):
    on = task["cfg"].get("amazon_badge", True)
    return tr(lang,
              f"🛒 <b>Amazon Logo</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "A small official <b>“available at amazon”</b> logo in the <b>top-left corner</b> of every "
              "Amazon post photo (Image Card and normal photo). It tells buyers the deal is on Amazon "
              "and builds trust.\n\n<i>Only on Amazon posts — never on other posts.</i>",
              f"🛒 <b>Amazon Logo</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "Har Amazon post ki photo ke <b>upar-left kone</b> mein chhota sa official "
              "<b>“available at amazon”</b> logo (Image Card aur normal photo dono pe). Isse buyer ko "
              "pata chalta hai ki deal Amazon ki hai aur bharosa badhta hai.\n\n"
              "<i>Sirf Amazon posts pe — baaki posts pe kabhi nahi.</i>")


def search_text(task, lang):
    on = task["cfg"].get("search_links")
    md = min_discount(task["cfg"])
    note = tr(lang,
              f"\n\n⚠️ <b>Discount Filter {md}%+ is ON</b> — these pages have no discount %, so they are "
              "<b>skipped while the filter is ON</b>, even if Search Links is ON. Turn the filter OFF to post them.",
              f"\n\n⚠️ <b>Discount Filter {md}%+ ON hai</b> — in pages ka discount % nahi hota, isliye "
              "<b>filter ON rehte ye skip honge</b>, chahe Search Links ON ho. Post karne hain to filter OFF karein."
              ) if md else ""
    return tr(lang,
              f"🔗 <b>Search Links</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "Amazon search / deals / offer page links (a list, not one product).\n"
              "<b>ON</b> → they are posted too, with your tag.\n<b>OFF</b> → they are skipped." + note,
              f"🔗 <b>Search Links</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "Amazon search / deals / offer page wale link (ek product nahi, list).\n"
              "<b>ON</b> → ye bhi aapke tag ke saath post honge.\n<b>OFF</b> → skip honge." + note)


def bold_text(task, lang):
    on = task["cfg"].get("bold_links", True)
    return tr(lang,
              f"🅱️ <b>Bold Link</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → links in the post are <b>bold</b> (stand out more).\n"
              "<b>OFF</b> → links are normal (not bold).\n\n"
              "<i>Works on every post — Amazon card link, original caption (MINIMAL) and Non-Amazon posts. "
              "Links still open directly either way.</i>",
              f"🅱️ <b>Bold Link</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → post ke links <b>bold</b> dikhenge (zyada nazar aate hain).\n"
              "<b>OFF</b> → links normal dikhenge (bold nahi).\n\n"
              "<i>Har post pe lagta hai — Amazon card ka link, original caption (MINIMAL) aur Non-Amazon posts. "
              "Dono mein link seedha khulta hai.</i>")


def promo_text(task, lang):
    on = task["cfg"].get("strip_promo", True)
    return tr(lang,
              f"🚫 <b>Remove t.me link &amp; Username</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → other channels' <b>@usernames</b> and <b>Telegram links</b> (t.me…) are removed "
              "from the post, including lines like \"Join @xyz for more\". Your own Draft / Destination "
              "channel is never removed.\n<b>OFF</b> → the caption keeps them as they are.",
              f"🚫 <b>Remove t.me link &amp; Username</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
              "<b>ON</b> → doosre channels ke <b>@username</b> aur <b>Telegram links</b> (t.me…) post se "
              "hat jaate hain, \"Join @xyz for more\" jaisi line bhi. Aapka apna Draft / Destination "
              "channel kabhi nahi hatta.\n<b>OFF</b> → caption mein jaise hain waise rahenge.")


# ── Discount Filter (sirf Amazon) ────────────────────────────────────────
DISCOUNT_OPTIONS = (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95)


def min_discount(cfg: dict) -> int:
    try:
        v = int(cfg.get("min_discount") or 0)
    except (TypeError, ValueError):
        return 0
    return v if 0 < v < 100 else 0


def disc_label(cfg: dict, lang: str) -> str:
    v = min_discount(cfg)
    return f"✅ {v}%+" if v else "❌ OFF"


def disc_text(task, lang):
    v = min_discount(task["cfg"])
    status = f"✅ {v}% {tr(lang, 'or more', 'ya zyada')}" if v else "❌ OFF"
    return tr(lang,
              f"📉 <b>Discount Filter</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{status}</b>\n\n"
              "Only for <b>Amazon</b> links. An Amazon deal with a smaller discount than this is "
              "<b>skipped</b> — not posted, no daily limit used.\n"
              "• Non-Amazon posts are never filtered here.\n"
              "• If Amazon gives no product details, the deal is skipped (you can send it again).\n"
              "• Amazon offer / category / search pages (no product) are skipped too.\n"
              "• MINIMAL mode: posted if at least one product in the post passes.\n\n"
              "<i>Choose the minimum discount:</i>",
              f"📉 <b>Discount Filter</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{status}</b>\n\n"
              "Sirf <b>Amazon</b> links pe. Jis Amazon deal ka discount isse kam hai wo "
              "<b>skip</b> hogi — post nahi hogi, daily limit bhi nahi kategi.\n"
              "• Non-Amazon posts pe ye filter nahi lagta.\n"
              "• Amazon se product details na mile to deal skip hogi (dobara bhej sakte hain).\n"
              "• Amazon offer / category / search page (bina product) bhi skip honge.\n"
              "• MINIMAL mode: post ka koi bhi ek product pass kare to post hogi.\n\n"
              "<i>Kam se kam kitna discount chahiye, chunein:</i>")


def disc_kb(task, lang):
    tid, cur = task["id"], min_discount(task["cfg"])
    opts = [btn(("✔️ " if v == cur else "") + (f"{v}%+" if v else "OFF"),
                GREEN if v == cur else "", callback_data=f"t:{tid}:disc:{v}")
            for v in DISCOUNT_OPTIONS]
    rows = [opts[i:i + 3] for i in range(0, len(opts), 3)]
    rows.append(back_row(tid, lang))
    return InlineKeyboardMarkup(rows)


def toggle_kb(task, action, on, lang, on_label=None, off_label=None):
    tid = task["id"]
    on_label = on_label or tr(lang, "🔴 Turn OFF", "🔴 Band karein")
    off_label = off_label or tr(lang, "🟢 Turn ON", "🟢 Chalu karein")
    return InlineKeyboardMarkup([
        [btn(on_label if on else off_label, RED if on else GREEN, callback_data=f"t:{tid}:{action}:t")],
        back_row(tid, lang),
    ])


# ── Post details ─────────────────────────────────────────────────────────
def amz_text(task, lang):
    c = task["cfg"]
    detailed, f = c.get("amz_detailed", True), c.get("amz_fields", {})
    if detailed:
        on_list = [FIELD_LABELS.get(k, k) for k in FIELD_ORDER if f.get(k)]
        body = tr(lang,
                  f"Mode: <b>✅ DETAILED</b>\n\n<b>Shown in the post:</b> {esc(', '.join(on_list)) or '—'}",
                  f"Mode: <b>✅ DETAILED</b>\n\n<b>Post mein dikhega:</b> {esc(', '.join(on_list)) or '—'}")
    else:
        body = tr(lang, "Mode: <b>MINIMAL</b> — the <b>original caption</b> is posted as it is "
                        "(\"Loot Free\", \"Apply Coupon\"…). Amazon links get your tag; other channels' "
                        "@usernames and Telegram links are removed. Photo: Image Card (if ON), else the "
                        "Amazon photo, else the original photo.",
                  "Mode: <b>MINIMAL</b> — <b>original caption</b> jaisa hai waisa jaata hai "
                  "(\"Loot Free\", \"Apply Coupon\"…). Amazon links pe aapka tag, doosre channel ke "
                  "@username aur Telegram links hat jaate hain. Photo: Image Card (ON ho to), warna "
                  "Amazon ki photo, warna original photo.")
    return tr(lang,
              f"🛍️ <b>Post Details</b> — {esc(tname(task, lang))}\n\n{body}\n\n"
              "<i>What Amazon posts show in the caption. Tap a field to turn it ON/OFF.</i>",
              f"🛍️ <b>Post Details</b> — {esc(tname(task, lang))}\n\n{body}\n\n"
              "<i>Amazon post ke caption mein kya dikhe. Kisi cheez pe tap karke ON/OFF karein.</i>")


def amz_kb(task, lang):
    tid, c = task["id"], task["cfg"]
    detailed, f = c.get("amz_detailed", True), c.get("amz_fields", {})
    rows = [[btn(tr(lang, "🔻 Switch to MINIMAL", "🔻 MINIMAL karein") if detailed
                 else tr(lang, "🔺 Switch to DETAILED", "🔺 DETAILED karein"),
                 callback_data=f"t:{tid}:amz:mode")]]
    if detailed:
        pair = []
        for k in ["image", "link"] + FIELD_ORDER:
            pair.append(btn(f"{_onoff(f.get(k))} {FIELD_LABELS.get(k, k)}", callback_data=f"t:{tid}:amzf:{k}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
    rows.append(back_row(tid, lang))
    return InlineKeyboardMarkup(rows)


# ── Buttons ──────────────────────────────────────────────────────────────
BTN_NAMES = {"buy": "⚡ Buy Now", "cart": "🛒 Add to Cart", "btn1": "📌 Button 1", "btn2": "📌 Button 2"}


def btns_text(task, lang):
    b = task["cfg"].get("buttons", {})

    def line(k):
        x = b.get(k, {})
        return f"{BTN_NAMES[k]} — {_onoff(x.get('enabled'))} <i>{esc(x.get('label', ''))}</i> {style_name(x.get('style', ''), lang)}"

    return tr(lang,
              f"🎛️ <b>Buttons under the post</b> — {esc(tname(task, lang))}\n\n"
              + "\n".join(line(k) for k in BTN_NAMES) +
              "\n\n<i>Buy Now / Add to Cart appear only on Amazon posts, with your tag. "
              "Button 1/2 are your own links (e.g. Join Channel). Each button can have its own colour.</i>",
              f"🎛️ <b>Post ke neeche Buttons</b> — {esc(tname(task, lang))}\n\n"
              + "\n".join(line(k) for k in BTN_NAMES) +
              "\n\n<i>Buy Now / Add to Cart sirf Amazon post pe, aapke tag ke saath. Button 1/2 aapke "
              "apne link (jaise Join Channel). Har button ka rang alag chun sakte hain.</i>")


def btns_kb(task, lang):
    tid = task["id"]
    return InlineKeyboardMarkup([
        [btn(BTN_NAMES["buy"], callback_data=f"t:{tid}:b:buy"),
         btn(BTN_NAMES["cart"], callback_data=f"t:{tid}:b:cart")],
        [btn(BTN_NAMES["btn1"], callback_data=f"t:{tid}:b:btn1"),
         btn(BTN_NAMES["btn2"], callback_data=f"t:{tid}:b:btn2")],
        back_row(tid, lang),
    ])


def one_btn_text(task, key, lang):
    b = task["cfg"].get("buttons", {}).get(key, {})
    lines = [f"{BTN_NAMES[key]} — {esc(tname(task, lang))}\n",
             f"{tr(lang, 'Label', 'Naam')}: <b>{esc(b.get('label', '-'))}</b>",
             f"Status: {_onoff(b.get('enabled'))}",
             f"{tr(lang, 'Colour', 'Rang')}: {style_name(b.get('style', ''), lang)}"]
    if key in ("btn1", "btn2"):
        lines.append(f"Link: <code>{esc(b.get('url') or tr(lang, 'not set', 'set nahi'))}</code>")
        lines.append(tr(lang, "\n<i>Shown under every post (e.g. 'Join Channel' → your channel).</i>",
                        "\n<i>Har post ke neeche dikhega (jaise 'Join Channel' → aapka channel).</i>"))
    elif key == "buy":
        lines.append(tr(lang, "\n<i>When ON, the link moves from the caption into this button.</i>",
                        "\n<i>ON karne pe link caption se hat ke is button mein aa jaata hai.</i>"))
    else:
        lines.append(tr(lang, "\n<i>Adds the product straight to the buyer's cart — longer commission window.</i>",
                        "\n<i>Product seedha customer ke cart mein jaata hai — kamai ka time zyada.</i>"))
    return "\n".join(lines)


def one_btn_kb(task, key, lang):
    tid = task["id"]
    b = task["cfg"].get("buttons", {}).get(key, {})
    on = b.get("enabled")
    rows = [[btn(tr(lang, "🔴 Turn OFF", "🔴 Band karein") if on else tr(lang, "🟢 Turn ON", "🟢 Chalu karein"),
                 RED if on else GREEN, callback_data=f"t:{tid}:b:{key}:on")],
            [btn(tr(lang, "📝 Change Label", "📝 Naam badlein"), callback_data=f"t:{tid}:b:{key}:label"),
             btn(f"🎨 {style_name(b.get('style', ''), lang)}", b.get("style", ""),
                 callback_data=f"t:{tid}:b:{key}:color")]]
    if key in ("btn1", "btn2"):
        rows.append([btn(tr(lang, "🔗 Set Link", "🔗 Link daalein"), callback_data=f"t:{tid}:b:{key}:link")])
    rows.append([btn(tr(lang, "⬅️ Buttons", "⬅️ Buttons"), callback_data=f"t:{tid}:btns")])
    return InlineKeyboardMarkup(rows)


# ── Header / Footer ──────────────────────────────────────────────────────
def hf_text(task, kind, lang):
    d = task["cfg"].get(kind, {})
    name = "Header" if kind == "header" else "Footer"
    where = tr(lang, "at the top of every post" if kind == "header" else "at the bottom of every post",
               "har post ke sabse upar" if kind == "header" else "har post ke sabse neeche")
    return tr(lang,
              f"{'🔝' if kind == 'header' else '🔚'} <b>{name}</b> — {esc(tname(task, lang))}\n\n"
              f"Status: {_onoff(d.get('enabled'))}\nText: <code>{esc(d.get('text') or '—')}</code>\n\n"
              f"<i>A line {where} (max 120 characters).</i>",
              f"{'🔝' if kind == 'header' else '🔚'} <b>{name}</b> — {esc(tname(task, lang))}\n\n"
              f"Status: {_onoff(d.get('enabled'))}\nText: <code>{esc(d.get('text') or '—')}</code>\n\n"
              f"<i>Ek line jo {where} lagti hai (max 120 character).</i>")


def hf_kb(task, kind, lang):
    tid = task["id"]
    on = task["cfg"].get(kind, {}).get("enabled")
    return InlineKeyboardMarkup([
        [btn(tr(lang, "✏️ Change Text", "✏️ Text badlein"), callback_data=f"t:{tid}:hf:{kind}:text"),
         btn(tr(lang, "🔴 Turn OFF", "🔴 Band karein") if on else tr(lang, "🟢 Turn ON", "🟢 Chalu karein"),
             RED if on else GREEN, callback_data=f"t:{tid}:hf:{kind}:on")],
        back_row(tid, lang),
    ])


# ── Watermark ────────────────────────────────────────────────────────────
WM_POS_EN = {"top": "⬆️ Top centre", "bottom_right": "↘️ Bottom right",
             "bottom_left": "↙️ Bottom left", "center": "⏺️ Centre"}


def wm_text(task, lang):
    wm = clean_watermark(task["cfg"].get("watermark"))
    pos = (WM_POS_EN if lang == "en" else WM_POSITIONS)[wm["position"]]
    c = task["cfg"]
    kinds = tr(lang,
               f"Shows on: 🛍️ Amazon {_onoff(c.get('wm_amazon', True))}   "
               f"📝 Non-Amazon {_onoff(c.get('wm_other', True))}\n",
               f"Kahan lagega: 🛍️ Amazon {_onoff(c.get('wm_amazon', True))}   "
               f"📝 Non-Amazon {_onoff(c.get('wm_other', True))}\n")
    return tr(lang,
              f"💧 <b>Watermark</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if wm['enabled'] else '❌ OFF'}</b>\n" + kinds +
              f"Text: <code>{esc(wm['text'] or 'not set')}</code>\n"
              f"Position: <b>{pos}</b>   Size: <b>{WM_SIZES[wm['size']][0]}</b>   "
              f"Colour: <b>{WM_COLORS[wm['color']][0]}</b>\n\n"
              "<i>Your name on the photo of every post (e.g. @MyDeals or 'Posted On My Deals') — "
              "on the Image Card and on normal photos. 'Top centre' looks like a clean title line.\n"
              "Tap 🛍️ Amazon / 📝 Non-Amazon to choose which posts get it.</i>",
              f"💧 <b>Watermark</b> — {esc(tname(task, lang))}\n\n"
              f"Status: <b>{'✅ ON' if wm['enabled'] else '❌ OFF'}</b>\n" + kinds +
              f"Text: <code>{esc(wm['text'] or 'set nahi')}</code>\n"
              f"Jagah: <b>{pos}</b>   Size: <b>{WM_SIZES[wm['size']][0]}</b>   "
              f"Rang: <b>{WM_COLORS[wm['color']][0]}</b>\n\n"
              "<i>Har post ki photo pe aapka naam (jaise @MyDeals ya 'Posted On My Deals') — Image Card "
              "aur normal photo dono pe. 'Upar beech' saaf title line jaisa dikhta hai.\n"
              "🛍️ Amazon / 📝 Non-Amazon dabake chunein ki kis post pe lage.</i>")


def wm_kb(task, lang):
    tid = task["id"]
    on = task["cfg"].get("watermark", {}).get("enabled")
    return InlineKeyboardMarkup([
        [btn(tr(lang, "🔴 Turn OFF", "🔴 Band karein") if on else tr(lang, "🟢 Turn ON", "🟢 Chalu karein"),
             RED if on else GREEN, callback_data=f"t:{tid}:wm:on")],
        [btn(tr(lang, "✏️ Text", "✏️ Text"), callback_data=f"t:{tid}:wm:text"),
         btn(tr(lang, "📍 Position", "📍 Jagah"), callback_data=f"t:{tid}:wmp:position")],
        [btn("🔠 Size", callback_data=f"t:{tid}:wmp:size"),
         btn(tr(lang, "🎨 Colour", "🎨 Rang"), callback_data=f"t:{tid}:wmp:color")],
        [btn(f"🛍️ Amazon {_onoff(task['cfg'].get('wm_amazon', True))}", callback_data=f"t:{tid}:wmk:amazon"),
         btn(f"📝 Non-Amazon {_onoff(task['cfg'].get('wm_other', True))}", callback_data=f"t:{tid}:wmk:other")],
        back_row(tid, lang),
    ])


def wm_pick_kb(task, field, lang):
    tid = task["id"]
    wm = clean_watermark(task["cfg"].get("watermark"))
    if field == "position":
        opts = WM_POS_EN if lang == "en" else WM_POSITIONS
    elif field == "size":
        opts = {k: v[0] for k, v in WM_SIZES.items()}
    else:
        opts = {k: v[0] for k, v in WM_COLORS.items()}
    rows, pair = [], []
    for val, label in opts.items():
        cur = wm[field] == val
        pair.append(btn(("✔️ " if cur else "") + label, GREEN if cur else "",
                        callback_data=f"t:{tid}:wms:{field}:{val}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn(tr(lang, "⬅️ Watermark", "⬅️ Watermark"), callback_data=f"t:{tid}:wm")])
    return InlineKeyboardMarkup(rows)


# =============================================================================
# NEW TASK
# =============================================================================
def upgrade_kb(lang):
    return InlineKeyboardMarkup([[btn(tr(lang, "💎 See Plans", "💎 Plans dekhein"), BLUE, callback_data="open_plan")]])


async def new_task(query, context, uid: int, lang: str):
    lim = limits(uid)
    if not is_active(uid):
        await show(query, context, tr(lang, "💎 You need an active plan to create tasks.",
                                      "💎 Task banane ke liye plan chahiye."), upgrade_kb(lang))
        return
    if not can_add_task(uid):
        await show(query, context,
                   tr(lang, f"🔒 Your <b>{lim['name']}</b> plan allows <b>{lim['tasks']}</b> task(s).\n"
                            "Upgrade to add more.",
                      f"🔒 Aapke <b>{lim['name']}</b> plan mein <b>{lim['tasks']}</b> task hi ban sakte hain.\n"
                      "Zyada ke liye upgrade karein."), upgrade_kb(lang))
        return
    n = len(list_tasks(uid)) + 1
    cfg = new_task_config(f"Task {n}")
    # Naya task pichle task ka tag le le — user ko dobara na daalna pade
    prev = list_tasks(uid)
    if prev:
        cfg["tag"] = prev[-1]["cfg"].get("tag", "")
    tid = create_task(uid, cfg)
    if not tid:
        await show(query, context, tr(lang, "❌ Could not create the task. Try again.",
                                      "❌ Task nahi bana. Dobara try karein."))
        return
    if not default_task(uid):
        set_default_task(uid, tid)
    if len(list_tasks(uid)) == 1:
        set_default_task(uid, tid)
    await notify_task_event(query.get_bot(), uid, get_task(tid, uid), "📋 <b>Naya task bana</b>")
    # Report — delete nahi hota
    await query.message.reply_text(
        tr(lang, f"✅ <b>{esc(cfg['name'])} created!</b>\nNow set its Destination and Tag below.",
           f"✅ <b>{esc(cfg['name'])} ban gaya!</b>\nAb neeche Destination aur Tag set karein."),
        parse_mode=ParseMode.HTML)
    task = get_task(tid, uid)
    m = await query.message.reply_text(task_text(uid, task, lang), parse_mode=ParseMode.HTML,
                                       reply_markup=task_kb(uid, task, lang), disable_web_page_preview=True)
    track(context, m)


# =============================================================================
# CALLBACKS
# =============================================================================
def _ask(context, action: str, tid: int, **extra):
    context.user_data["action"] = action
    context.user_data["tid"] = tid
    for k, v in extra.items():
        context.user_data[k] = v


async def handle_task_callback(query, context, uid: int, data: str) -> bool:
    lang = get_lang(uid)

    if data == "tl":
        context.user_data.pop("action", None)
        await show(query, context, tasks_text(uid, lang), tasks_kb(uid, lang))
        return True
    if data == "tn":
        await new_task(query, context, uid, lang)
        return True
    if data.startswith("ch:"):
        await _channel_added_choice(query, context, uid, data, lang)
        return True
    if not data.startswith("t:"):
        return False

    parts = data.split(":")
    try:
        tid = int(parts[1])
    except (IndexError, ValueError):
        return True
    task = get_task(tid, uid)
    if not task:
        await show(query, context, tr(lang, "⚠️ This task no longer exists.", "⚠️ Ye task ab nahi hai."),
                   tasks_kb(uid, lang))
        return True
    cfg = task["cfg"]
    act = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""
    arg2 = parts[4] if len(parts) > 4 else ""

    def save():
        save_task(uid, tid, cfg)
        task["cfg"] = cfg

    if act == "":
        context.user_data.pop("action", None)
        await show(query, context, task_text(uid, task, lang), task_kb(uid, task, lang))
        return True

    if act == "disc":
        if arg.isdigit() and int(arg) in DISCOUNT_OPTIONS:
            old = min_discount(cfg)
            cfg["min_discount"] = int(arg)
            save()
            if old != int(arg):
                await query.answer(tr(lang, f"📉 Discount Filter: {disc_label(cfg, lang)}",
                                      f"📉 Discount Filter: {disc_label(cfg, lang)}"))
        await show(query, context, disc_text(task, lang), disc_kb(task, lang))
        return True

    if act == "card":
        import card_ui
        await card_ui.handle(query, context, uid, task, parts[3:], lang)
        return True

    if act == "tag":
        _ask(context, "t_tag", tid)
        await show(query, context, tag_text(task, lang), InlineKeyboardMarkup([back_row(tid, lang)]))
        return True

    if act in ("dest", "src"):
        _ask(context, "t_dest" if act == "dest" else "t_src", tid)
        await show(query, context, chan_text(task, act, lang), chan_kb(task, act, lang))
        await show_picker(context, query.message, act, lang)
        return True
    if act == "src_off":
        cfg.update(source_channel="", source_title="", source_username="")
        save()
        context.user_data.pop("action", None)
        await show(query, context, chan_text(task, "src", lang), chan_kb(task, "src", lang))
        return True

    if act == "filt":
        if arg in ("amz", "oth"):
            key = "allow_amazon" if arg == "amz" else "allow_other"
            other = "allow_other" if arg == "amz" else "allow_amazon"
            if cfg.get(key, True) and not cfg.get(other, True):
                await query.answer(tr(lang, "At least one must stay ON.", "Kam se kam ek ON rehna chahiye."),
                                   show_alert=True)
                return True
            cfg[key] = not cfg.get(key, True)
            save()
        await show(query, context, filt_text(task, lang), filt_kb(task, lang))
        return True

    for a, key, txt in (("dup", "dup_check", dup_text), ("silent", "silent", silent_text),
                        ("search", "search_links", search_text), ("promo", "strip_promo", promo_text),
                        ("badge", "amazon_badge", badge_text), ("bold", "bold_links", bold_text)):
        if act == a:
            default = a != "search"
            if arg == "t":
                cfg[key] = not cfg.get(key, default)
                save()
            on = cfg.get(key, default)
            if a == "silent":
                kb = toggle_kb(task, a, on, lang, on_label=tr(lang, "🔔 Make Loud", "🔔 Loud karein"),
                               off_label=tr(lang, "🔕 Make Silent", "🔕 Silent karein"))
            else:
                kb = toggle_kb(task, a, on, lang)
            await show(query, context, txt(task, lang), kb)
            return True

    if act == "amz":
        if arg == "mode":
            cfg["amz_detailed"] = not cfg.get("amz_detailed", True)
            save()
        await show(query, context, amz_text(task, lang), amz_kb(task, lang))
        return True
    if act == "amzf" and arg in FIELD_LABELS:
        f = cfg.setdefault("amz_fields", {})
        f[arg] = not f.get(arg, False)
        save()
        await show(query, context, amz_text(task, lang), amz_kb(task, lang))
        return True

    if act == "btns":
        await show(query, context, btns_text(task, lang), btns_kb(task, lang))
        return True
    if act == "b" and arg in BTN_NAMES:
        b = cfg.setdefault("buttons", {}).setdefault(arg, {})
        if arg2 == "on":
            if arg in ("btn1", "btn2") and not b.get("enabled") and not b.get("url"):
                _ask(context, "t_btn_link", tid, bkey=arg)
                await show(query, context, tr(lang, "🔗 First send the link for this button "
                                                    "(https://... or @channel):",
                                              "🔗 Pehle is button ka link bhejein (https://... ya @channel):"),
                           InlineKeyboardMarkup([back_row(tid, lang)]))
                return True
            b["enabled"] = not b.get("enabled")
            save()
        elif arg2 == "color":
            b["style"] = next_style(b.get("style", ""))
            save()
        elif arg2 == "label":
            _ask(context, "t_btn_label", tid, bkey=arg)
            await show(query, context, tr(lang, "📝 Send the new button label (max 20 characters):",
                                          "📝 Button ka naya naam bhejein (max 20 character):"),
                       InlineKeyboardMarkup([back_row(tid, lang)]))
            return True
        elif arg2 == "link" and arg in ("btn1", "btn2"):
            _ask(context, "t_btn_link", tid, bkey=arg)
            await show(query, context, tr(lang, "🔗 Send the link (https://... or @channel):",
                                          "🔗 Link bhejein (https://... ya @channel):"),
                       InlineKeyboardMarkup([back_row(tid, lang)]))
            return True
        await show(query, context, one_btn_text(task, arg, lang), one_btn_kb(task, arg, lang))
        return True

    if act == "hf" and arg in ("header", "footer"):
        d = cfg.setdefault(arg, {})
        if arg2 == "on":
            if not d.get("enabled") and not (d.get("text") or "").strip():
                arg2 = "text"
            else:
                d["enabled"] = not d.get("enabled")
                save()
        if arg2 == "text":
            _ask(context, "t_hf", tid, hf_kind=arg)
            await show(query, context, tr(lang, f"✏️ Send the {arg} text (max 120 characters).\n"
                                                f"<i>Send <code>-</code> to remove it.</i>",
                                          f"✏️ {arg.title()} ka text bhejein (max 120 character).\n"
                                          f"<i>Hatana ho to <code>-</code> bhejein.</i>"),
                       InlineKeyboardMarkup([back_row(tid, lang)]))
            return True
        await show(query, context, hf_text(task, arg, lang), hf_kb(task, arg, lang))
        return True

    if act == "wm":
        wm = cfg["watermark"] = clean_watermark(cfg.get("watermark"))
        if arg == "on":
            if not wm["enabled"] and not wm["text"].strip():
                arg = "text"
            else:
                wm["enabled"] = not wm["enabled"]
                save()
        if arg == "text":
            _ask(context, "t_wm", tid)
            await show(query, context, tr(lang, "✏️ Send the watermark text (max 40 characters).\n"
                                                "Example: <code>@MyDeals</code> or <code>Posted On My Deals</code>",
                                          "✏️ Watermark ka text bhejein (max 40 character).\n"
                                          "Jaise: <code>@MyDeals</code> ya <code>Posted On My Deals</code>"),
                       InlineKeyboardMarkup([back_row(tid, lang)]))
            return True
        await show(query, context, wm_text(task, lang), wm_kb(task, lang))
        return True
    if act == "wmk" and arg in ("amazon", "other"):
        key = "wm_amazon" if arg == "amazon" else "wm_other"
        cfg[key] = not cfg.get(key, True)
        save()
        await query.answer(tr(lang,
                              f"💧 Watermark on {'Amazon' if arg == 'amazon' else 'Non-Amazon'} posts: "
                              f"{'ON' if cfg[key] else 'OFF'}",
                              f"💧 {'Amazon' if arg == 'amazon' else 'Non-Amazon'} posts pe watermark: "
                              f"{'ON' if cfg[key] else 'OFF'}"))
        await show(query, context, wm_text(task, lang), wm_kb(task, lang))
        return True
    if act == "wmp" and arg in ("position", "size", "color"):
        title = {"position": tr(lang, "📍 Where should the watermark go?", "📍 Watermark kahan lage?"),
                 "size": tr(lang, "🔠 Watermark size", "🔠 Watermark ka size"),
                 "color": tr(lang, "🎨 Watermark text colour", "🎨 Watermark text ka rang")}[arg]
        await show(query, context, title, wm_pick_kb(task, arg, lang))
        return True
    if act == "wms" and arg in ("position", "size", "color"):
        wm = cfg["watermark"] = clean_watermark(cfg.get("watermark"))
        valid = {"position": WM_POSITIONS, "size": WM_SIZES, "color": WM_COLORS}[arg]
        if arg2 in valid:
            wm[arg] = arg2
            save()
        await show(query, context, wm_text(task, lang), wm_kb(task, lang))
        return True

    if act == "def":
        set_default_task(uid, tid)
        await query.answer(tr(lang, "⭐ Default task set", "⭐ Default task set ho gaya"))
        task = get_task(tid, uid)
        await show(query, context, task_text(uid, task, lang), task_kb(uid, task, lang))
        return True

    if act == "pause":
        set_task_paused(uid, tid, True)
        task = get_task(tid, uid)
        await show(query, context, task_text(uid, task, lang), task_kb(uid, task, lang))
        return True
    if act == "resume":
        await _resume(query, context, uid, task, lang, swap_with=int(arg) if arg.isdigit() else None)
        return True

    if act == "ren":
        _ask(context, "t_name", tid)
        await show(query, context, tr(lang, f"✏️ Send a new name for this task (max {MAX_TASK_NAME} characters):",
                                      f"✏️ Task ka naya naam bhejein (max {MAX_TASK_NAME} character):"),
                   InlineKeyboardMarkup([back_row(tid, lang)]))
        return True

    if act == "del":
        if arg == "ok":
            name = tname(task, lang)
            await notify_task_event(query.get_bot(), uid, task, "🗑️ <b>Task delete hua</b>")
            delete_task(uid, tid)
            await query.message.reply_text(tr(lang, f"🗑️ <b>{esc(name)}</b> deleted.",
                                              f"🗑️ <b>{esc(name)}</b> delete ho gaya."),
                                           parse_mode=ParseMode.HTML)
            await show(query, context, tasks_text(uid, lang), tasks_kb(uid, lang))
            return True
        await show(query, context,
                   tr(lang, f"🗑️ Delete <b>{esc(tname(task, lang))}</b>? All its settings will be removed.",
                      f"🗑️ <b>{esc(tname(task, lang))}</b> delete karein? Iski saari settings hat jayengi."),
                   InlineKeyboardMarkup([[btn(tr(lang, "✅ Yes, delete", "✅ Haan, delete"), RED,
                                              callback_data=f"t:{tid}:del:ok"),
                                          btn(tr(lang, "❌ No", "❌ Nahi"), callback_data=f"t:{tid}")]]))
        return True

    return True


async def _resume(query, context, uid, task, lang, swap_with=None):
    tid = task["id"]
    lim = limits(uid)["tasks"]
    if lim <= 0:
        await show(query, context, tr(lang, "💎 You need an active plan.", "💎 Plan chahiye."), upgrade_kb(lang))
        return
    if swap_with:
        set_task_paused(uid, swap_with, True)
    running = [t for t in list_tasks(uid) if not t["paused"]]
    if len(running) >= lim:
        rows = [[btn(tr(lang, f"⏸️ Pause {tname(t, lang)}", f"⏸️ {tname(t, lang)} rokein"),
                     callback_data=f"t:{tid}:resume:{t['id']}")] for t in running]
        rows.append(back_row(tid, lang))
        await show(query, context,
                   tr(lang, f"🔒 Your plan allows <b>{lim}</b> running task(s). Which one should be paused "
                            f"so <b>{esc(tname(task, lang))}</b> can run?",
                      f"🔒 Aapke plan mein <b>{lim}</b> task hi chal sakte hain. <b>{esc(tname(task, lang))}</b> "
                      f"chalane ke liye kaunsa rokein?"),
                   InlineKeyboardMarkup(rows))
        return
    set_task_paused(uid, tid, False)
    task = get_task(tid, uid)
    await show(query, context, task_text(uid, task, lang), task_kb(uid, task, lang))


# =============================================================================
# BOT KO CHANNEL MEIN ADMIN BANAYA
# =============================================================================
async def channel_added_prompt(bot, uid: int, chat, can_post: bool):
    lang = get_lang(uid)
    rows = []
    for t in list_tasks(uid):
        n = tname(t, lang)
        rows.append([btn(f"📢 {n} — Destination", callback_data=f"ch:d:{chat.id}:{t['id']}"),
                     btn(f"📥 {n} — Draft", callback_data=f"ch:s:{chat.id}:{t['id']}")])
    if can_add_task(uid):
        rows.append([btn(tr(lang, "➕ New Task with this Destination", "➕ Naya Task (ye Destination)"),
                         GREEN, callback_data=f"ch:d:{chat.id}:new")])
    rows.append([btn(tr(lang, "❌ Nothing", "❌ Kuch nahi"), callback_data="home")])
    note = "" if can_post else tr(lang, "\n\n⚠️ <i>'Post Messages' is OFF — turn it ON to use it as a Destination.</i>",
                                  "\n\n⚠️ <i>'Post Messages' permission band hai — Destination ke liye ON karein.</i>")
    try:
        await bot.send_message(
            uid,
            tr(lang, f"📢 You made me an admin in {chan(chat.title, chat.username)}!\n\nWhere should I use it?",
               f"📢 Aapne mujhe {chan(chat.title, chat.username)} mein admin banaya!\n\nIse kahan use karein?")
            + note,
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
    except Exception as e:
        logger.error(f"Channel prompt fail ({uid}): {e}")


async def _channel_added_choice(query, context, uid, data, lang):
    parts = data.split(":")
    if len(parts) != 4:
        return
    _, kind, cid, target = parts
    try:
        cid = int(cid)
    except ValueError:
        return
    if target == "new":
        if not is_active(uid) or not can_add_task(uid):
            await show(query, context, tr(lang, "🔒 You can't add more tasks on your plan.",
                                          "🔒 Aapke plan mein aur task nahi ban sakte."), upgrade_kb(lang))
            return
        prev = list_tasks(uid)
        cfg = new_task_config(f"Task {len(prev) + 1}")
        if prev:
            cfg["tag"] = prev[-1]["cfg"].get("tag", "")
        tid = create_task(uid, cfg)
        if not tid:
            return
        if not default_task(uid):
            set_default_task(uid, tid)
        await notify_task_event(query.get_bot(), uid, get_task(tid, uid), "📋 <b>Naya task bana</b>")
    else:
        try:
            tid = int(target)
        except ValueError:
            return
    ok, text = await set_task_channel(query.get_bot(), uid, tid, cid, "dest" if kind == "d" else "src", lang)
    task = get_task(tid, uid)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)
    if task:
        m = await query.message.reply_text(task_text(uid, task, lang), parse_mode=ParseMode.HTML,
                                           reply_markup=task_kb(uid, task, lang), disable_web_page_preview=True)
        track(context, m)


# =============================================================================
# TEXT INPUT — True = handle ho gaya
# =============================================================================
async def handle_task_input(update: Update, context, uid: int, action: str) -> bool:
    if not action.startswith("t_"):
        return False
    msg = update.message
    lang = get_lang(uid)
    text = (msg.text or "").strip()
    tid = context.user_data.get("tid")
    task = get_task(tid, uid) if tid else None
    track(context, msg)

    async def reply(t, kb=None):
        m = await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        track(context, m)

    if not task:
        context.user_data.pop("action", None)
        await reply(tr(lang, "⚠️ Task not found. Open /tasks again.", "⚠️ Task nahi mila. /tasks dobara kholein."))
        return True
    cfg = task["cfg"]

    def done():
        context.user_data.pop("action", None)
        save_task(uid, tid, cfg)
        task["cfg"] = cfg

    if action == "t_tag":
        if not is_valid_tag(text):
            await reply(tr(lang, "⚠️ This doesn't look like a valid tag.\nAn Amazon India tag looks like "
                                 "<code>mydeals-21</code> (ends with <b>-21</b>). Please send it again.",
                           "⚠️ Ye tag sahi nahi lag raha.\nAmazon India tag aisa hota hai: <code>mydeals-21</code> "
                           "(aakhir mein <b>-21</b>). Dobara bhejein."))
            return True
        old_tag = cfg.get("tag") or ""
        cfg["tag"] = text
        done()
        if old_tag != text:
            await notify_task_event(context.bot, uid, task, "🏷️ <b>Tag badla</b>"
                                    + (f" (pehle <code>{esc(old_tag)}</code>)" if old_tag else ""))
        await reply(tr(lang, f"✅ <b>Tag saved:</b> <code>{esc(text)}</code>",
                       f"✅ <b>Tag save ho gaya:</b> <code>{esc(text)}</code>"))
        await reply(task_text(uid, task, lang), task_kb(uid, task, lang))
        return True

    if action in ("t_dest", "t_src"):
        ident = channel_from_message(msg) or normalize_channel_ident(text)
        if not ident:
            kind = "dest" if action == "t_dest" else "src"
            if "t.me/+" in text or "joinchat" in text:
                t = tr(lang, "⚠️ Invite links (t.me/+…) can't be used to find a channel.\n"
                             "👇 Tap <b>Choose Channel</b> below and pick it — private channels work.",
                       "⚠️ Invite link (t.me/+…) se channel nahi milta.\n"
                       "👇 Neeche <b>Channel chunein</b> dabake channel chunein — private channel bhi chalega.")
            else:
                t = tr(lang, "⚠️ I couldn't read the channel.\n👇 Tap <b>Choose Channel</b> below and pick it, "
                             "or send its @username.",
                       "⚠️ Channel samajh nahi aaya.\n👇 Neeche <b>Channel chunein</b> dabake chunein, "
                       "ya channel ka @username bhejein.")
            m = await msg.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=picker_kb(kind, lang))
            context.user_data["picker_on"] = True
            track(context, m)
            return True
        ok, out = await set_task_channel(context.bot, uid, tid, ident, "dest" if action == "t_dest" else "src", lang)
        if ok:
            context.user_data.pop("action", None)
            context.user_data.pop("picker_on", None)
            await msg.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove())  # report
            task = get_task(tid, uid)
            await reply(task_text(uid, task, lang), task_kb(uid, task, lang))
        else:
            await reply(out)
        return True

    if action == "t_name":
        if not text or len(text) > MAX_TASK_NAME:
            await reply(tr(lang, f"⚠️ 1 to {MAX_TASK_NAME} characters please.",
                           f"⚠️ 1 se {MAX_TASK_NAME} character ke beech bhejein."))
            return True
        cfg["name"] = text
        done()
        await reply(task_text(uid, task, lang), task_kb(uid, task, lang))
        return True

    if action == "t_hf":
        kind = context.user_data.get("hf_kind", "header")
        if len(text) > 120:
            await reply(tr(lang, "⚠️ Max 120 characters.", "⚠️ Max 120 character."))
            return True
        val = "" if text == "-" else text
        cfg[kind] = {"enabled": bool(val), "text": val}
        done()
        await reply(hf_text(task, kind, lang), hf_kb(task, kind, lang))
        return True

    if action == "t_wm":
        if not text or len(text) > 40:
            await reply(tr(lang, "⚠️ 1 to 40 characters please.", "⚠️ 1 se 40 character ke beech bhejein."))
            return True
        wm = cfg["watermark"] = clean_watermark(cfg.get("watermark"))
        wm.update(text=text, enabled=True)
        done()
        await reply(wm_text(task, lang), wm_kb(task, lang))
        return True

    if action == "t_btn_label":
        key = context.user_data.get("bkey", "btn1")
        if not text or len(text) > 20:
            await reply(tr(lang, "⚠️ 1 to 20 characters please.", "⚠️ 1 se 20 character ke beech bhejein."))
            return True
        cfg.setdefault("buttons", {}).setdefault(key, {})["label"] = text
        done()
        await reply(one_btn_text(task, key, lang), one_btn_kb(task, key, lang))
        return True

    if action == "t_btn_link":
        key = context.user_data.get("bkey", "btn1")
        link = text
        if link.lower().startswith(("t.me/", "telegram.me/", "www.")):
            link = "https://" + link
        elif link.startswith("@") and len(link) > 4:
            link = "https://t.me/" + link[1:]
        if not _BTN_URL_RE.match(link) or len(link) > 500:
            await reply(tr(lang, "⚠️ This link doesn't look right.\nSend like: <code>https://t.me/mychannel</code> "
                                 "or <code>@mychannel</code>",
                           "⚠️ Ye link sahi nahi lag raha.\nAise bhejein: <code>https://t.me/mychannel</code> "
                           "ya <code>@mychannel</code>"))
            return True
        b = cfg.setdefault("buttons", {}).setdefault(key, {})
        b.update(url=link, enabled=True)
        done()
        await reply(one_btn_text(task, key, lang), one_btn_kb(task, key, lang))
        return True

    return False


# =============================================================================
# /config — task ki SAARI settings ek nazar mein
# =============================================================================
def config_text(uid: int, task: dict, lang: str) -> str:
    from card import CARD_CHOICES, WM_POSITIONS, WM_SIZES, WM_COLORS
    from database import posts_today
    c = task["cfg"]
    lim = limits(uid)
    run = task["id"] in running_ids(uid)
    d = default_task(uid)
    on = lambda v: "✅" if v else "❌"      # noqa: E731
    val = lambda v: f"<code>{esc(str(v))}</code>" if v else "❌"      # noqa: E731

    state = tr(lang, "▶️ Running", "▶️ Chal raha hai") if run else tr(lang, "⏸️ Paused", "⏸️ Ruka hua")
    star = "  ⭐ Default" if d and d["id"] == task["id"] else ""
    cap = "∞" if lim.get("key") == "admin" else lim.get("daily", 0)

    lines = [f"⚙️ <b>Config — {esc(tname(task, lang))}</b>  {state}{star}\n",
             f"🏷️ Affiliate Tag: {val(c.get('tag'))}",
             f"📥 Draft: {chan(c.get('source_title'), c.get('source_username'), '❌')}",
             f"📢 Destination: {chan(c.get('channel_title'), c.get('channel_username'), '❌')}",
             f"📤 {tr(lang, 'Today', 'Aaj')}: {posts_today(uid, task['id'])} / {cap}\n",
             f"🛍️ Amazon posts: {on(c.get('allow_amazon', True))}",
             f"📝 Non-Amazon posts: {on(c.get('allow_other', True))}",
             f"♻️ Duplicate: {on(c.get('dup_check', True))}",
             f"🚫 Remove t.me link &amp; Username: {on(c.get('strip_promo', True))}",
             f"🅱️ Bold Link: {on(c.get('bold_links', True))}",
             f"🔗 Search Links: {on(c.get('search_links'))}",
             f"📉 Discount Filter: {disc_label(c, lang)}",
             f"🔔 Notification: {'🔕 Silent' if c.get('silent', True) else '🔔 Loud'}\n"]

    f = c.get("amz_fields", {})
    if c.get("amz_detailed", True):
        shown = ", ".join(FIELD_LABELS.get(k, k) for k in ["image", "link"] + FIELD_ORDER if f.get(k)) or "—"
        lines.append(f"🛍️ Post Details: <b>DETAILED</b> — {esc(shown)}")
    else:
        lines.append(f"🛍️ Post Details: <b>MINIMAL</b> — " + tr(lang, "original caption", "original caption"))

    card = c.get("card", {})
    if not lim.get("card"):
        lines.append("🎨 Image Card: 🔒 Pro")
    elif card.get("enabled"):
        pick = lambda k: CARD_CHOICES.get(k, {}).get(card.get(k), card.get(k) or "—")      # noqa: E731
        lines.append(f"🎨 Image Card: ✅ — {esc(str(pick('theme')))}, {esc(str(pick('image_size')))}, "
                     f"{esc(str(pick('font')))}")
        lines.append(f"     Price {on(card.get('show_price'))}  MRP {on(card.get('show_mrp'))}  "
                     f"Discount {on(card.get('show_discount'))}  Rating {on(card.get('show_rating'))}")
    else:
        lines.append("🎨 Image Card: ❌")

    wm = c.get("watermark", {})
    if wm.get("enabled") and (wm.get("text") or "").strip():
        def nm(table, k):
            v = table.get(k, k)
            return v[0] if isinstance(v, tuple) else v
        lines.append(f"💧 Watermark: ✅ {val(wm.get('text'))} — "
                     f"{esc(str(nm(WM_POSITIONS, wm.get('position'))))}, "
                     f"{esc(str(nm(WM_SIZES, wm.get('size'))))}, "
                     f"{esc(str(nm(WM_COLORS, wm.get('color'))))}")
    else:
        lines.append("💧 Watermark: ❌")

    for key, label in (("header", "🔝 Header"), ("footer", "🔚 Footer")):
        h = c.get(key, {})
        text = (h.get("text") or "").strip()
        lines.append(f"{label}: ✅ {val(text[:60] + ('…' if len(text) > 60 else ''))}"
                     if h.get("enabled") and text else f"{label}: ❌")

    b = c.get("buttons", {})
    lines.append("\n🎛️ <b>Buttons</b>")
    for key in ("buy", "cart", "btn1", "btn2"):
        x = b.get(key, {})
        name = esc(x.get("label") or key)
        link = f" → {esc(x.get('url'))}" if key in ("btn1", "btn2") and x.get("url") else ""
        lines.append(f"   {on(x.get('enabled'))} {name}{link}")
    return "\n".join(lines)


def config_kb(uid: int, task: dict, lang: str) -> InlineKeyboardMarkup:
    rows = [[btn(tr(lang, "✏️ Change settings", "✏️ Settings badlein"), BLUE, callback_data=f"t:{task['id']}")]]
    others = [t for t in list_tasks(uid) if t["id"] != task["id"]]
    pair = []
    for t in others[:8]:
        pair.append(btn(f"⚙️ {tname(t, lang)}", callback_data=f"cfg:{t['id']}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🏠 Home", callback_data="home")])
    return InlineKeyboardMarkup(rows)

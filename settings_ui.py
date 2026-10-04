"""
settings_ui.py — har user ki settings: affiliate tag, channel, draft channel,
post details, buttons, header/footer, watermark, silent, park mode, search
links, price-drop auto-post. Commands, buttons aur text input sab yahin.
"""
import re
import json
import html as html_lib
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from amazon_api import is_valid_tag
from caption import FIELD_LABELS, FIELD_ORDER
from database import queue_counts, queue_fetch_all, queue_clear
from engine import (
    flush_queue, next_batch_label, now_local, hhmm, same_channel,
    QUEUE_MAX_AGE_HOURS, POST_GAP_SECONDS,
)
from storage import load_config, save_config, find_users_by_source, utcnow, TZ_NAME

logger = logging.getLogger(__name__)

esc = html_lib.escape


def _onoff(v) -> str:
    return "✅" if v else "❌"


BACK_ROW = [InlineKeyboardButton("⬅️ Settings", callback_data="set_home"),
            InlineKeyboardButton("❌ Close", callback_data="cancel")]


# =============================================================================
# CHANNEL VERIFY
# =============================================================================
def channel_from_message(msg):
    """
    Forward kiye message se channel ID nikalo. Telegram ab 'forward_origin' bhejta hai,
    jo is library version mein api_kwargs mein aata hai — teeno jagah dekho.
    Returns channel id (int) ya None.
    """
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
    """@name / name / t.me/name / https://t.me/name / -100… → get_chat ke layak."""
    t = (text or "").strip()
    if not t:
        return None
    if t.lstrip("-").isdigit():
        return t
    m = _TME_RE.match(t)
    if m:
        return "@" + m.group(1)
    if "t.me/+" in t or "joinchat" in t:
        return None          # private invite link se channel nahi milta
    t = t.lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", t):
        return "@" + t
    return None


async def verify_channel(bot, ident, uid: int, need_post: bool = True):
    """
    Check: channel hai, bot admin hai (post permission), aur user khud bhi admin hai.
    Returns (chat_id_str, title, error_text_or_None)
    """
    try:
        chat = await bot.get_chat(ident)
    except Exception:
        return None, None, ("Channel nahi mila. Pehle bot ko channel mein admin banao, "
                            "phir @username ya channel ID bhejo.")
    if chat.type != "channel":
        return None, None, "Ye channel nahi hai. Sirf Telegram channel chalega."
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
    except Exception:
        return None, None, "Bot is channel mein admin nahi hai. Pehle admin banao."
    if me.status != "administrator":
        return None, None, "Bot is channel mein admin nahi hai. Pehle admin banao."
    if need_post and not getattr(me, "can_post_messages", False):
        return None, None, "Bot admin hai par 'Post Messages' permission band hai — ON karo."
    try:
        mem = await bot.get_chat_member(chat.id, uid)
        if mem.status not in ("creator", "administrator"):
            return None, None, "Tum is channel ke admin nahi ho — sirf apna channel jod sakte ho."
    except Exception:
        return None, None, "Tum is channel ke admin nahi lag rahe — sirf apna channel jod sakte ho."
    return str(chat.id), chat.title or str(chat.id), None


async def save_post_channel(bot, uid: int, ident) -> str:
    cid, title, err = await verify_channel(bot, ident, uid, need_post=True)
    if err:
        return f"❌ {err}"
    cfg = load_config(uid)
    if same_channel(cid, cfg.get("source_channel")):
        return "⚠️ Ye tumhara draft channel hai! Post channel alag hona chahiye."
    cfg["channel"], cfg["channel_title"] = cid, title
    if not save_config(uid, cfg):
        return "❌ Save nahi hua, dobara try karo."
    return (f"✅ <b>Post channel set ho gaya!</b>\n📢 <b>{esc(title)}</b>\n\n"
            f"Ab mujhe koi bhi Amazon link bhejo — main is channel pe post kar dunga.")


async def save_source_channel(bot, uid: int, ident) -> str:
    cid, title, err = await verify_channel(bot, ident, uid, need_post=False)
    if err:
        return f"❌ {err}"
    cfg = load_config(uid)
    if same_channel(cid, cfg.get("channel")):
        return "⚠️ Ye tumhara post channel hai! Draft channel alag hona chahiye."
    owners = [o for o in find_users_by_source(int(cid)) if o != uid]
    if owners:
        return "⚠️ Ye channel pehle se kisi aur account ka draft channel hai."
    cfg["source_channel"], cfg["source_title"] = cid, title
    if not save_config(uid, cfg):
        return "❌ Save nahi hua, dobara try karo."
    return (f"✅ <b>Draft channel set ho gaya!</b>\n📥 <b>{esc(title)}</b>\n\n"
            f"Ab is channel mein deal daalo — main khud utha ke post channel pe bhej dunga.")


# =============================================================================
# TEXT BUILDERS
# =============================================================================
def settings_home_text(cfg: dict) -> str:
    return (
        "⚙️ <b>Settings</b>\n\n"
        f"🏷️ Affiliate Tag : <code>{esc(cfg.get('tag') or 'set nahi')}</code>\n"
        f"📢 Post Channel  : <b>{esc(cfg.get('channel_title') or cfg.get('channel') or 'set nahi')}</b>\n"
        f"📥 Draft Channel : <b>{esc(cfg.get('source_title') or cfg.get('source_channel') or 'band')}</b>\n\n"
        "Neeche se jo badalna hai wo chuno 👇"
    )


def settings_home_kb(cfg: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷️ Affiliate Tag", callback_data="set_tag"),
         InlineKeyboardButton("📢 Post Channel", callback_data="set_channel")],
        [InlineKeyboardButton("📥 Draft Channel", callback_data="set_source"),
         InlineKeyboardButton("🛍️ Post Details", callback_data="set_amz")],
        [InlineKeyboardButton("🎛️ Buttons", callback_data="sb_main"),
         InlineKeyboardButton("🖼️ Watermark", callback_data="set_wm")],
        [InlineKeyboardButton("🔝 Header", callback_data="set_header"),
         InlineKeyboardButton("🔚 Footer", callback_data="set_footer")],
        [InlineKeyboardButton("🔔 Notification", callback_data="set_silent"),
         InlineKeyboardButton("🅿️ Park Post", callback_data="set_park")],
        [InlineKeyboardButton(f"{_onoff(cfg.get('search_links'))} Search Links",
                              callback_data="set_search")],
        [InlineKeyboardButton(f"{_onoff(cfg.get('pricedrop_autopost'))} Price Drop Auto-Post",
                              callback_data="set_pdauto")],
        [InlineKeyboardButton("❌ Close", callback_data="cancel")],
    ])


def tag_text(cfg: dict) -> str:
    return (
        "🏷️ <b>Affiliate Tag</b>\n\n"
        f"Abhi: <code>{esc(cfg.get('tag') or 'set nahi hai')}</code>\n\n"
        "Har post ke Amazon link mein <b>tumhara</b> tag lagega, taaki kamai tumhe mile.\n\n"
        "<i>Tag kahan milega? Amazon Associates (affiliate-program.amazon.in) mein login "
        "karo — upar right corner mein dikhta hai, jaise <code>mydeals-21</code></i>"
    )


def tag_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Tag badlo", callback_data="set_tag_edit")],
        BACK_ROW,
    ])


def channel_text(cfg: dict) -> str:
    return (
        "📢 <b>Post Channel</b>\n\n"
        f"Abhi: <b>{esc(cfg.get('channel_title') or cfg.get('channel') or 'set nahi hai')}</b>\n\n"
        "<b>Kaise jode:</b>\n"
        "1️⃣ Mujhe apne channel mein <b>admin</b> banao ('Post Messages' ON rakho)\n"
        "2️⃣ Main khud pooch lunga — bas button dabana hai\n\n"
        "<i>Ya neeche 'Channel badlo' dabao aur channel ka @username / ID bhejo, "
        "ya channel ka koi message forward kar do.</i>"
    )


def channel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Channel badlo", callback_data="set_channel_edit")],
        BACK_ROW,
    ])


def source_text(cfg: dict) -> str:
    return (
        "📥 <b>Draft Channel</b> (optional)\n\n"
        f"Abhi: <b>{esc(cfg.get('source_title') or cfg.get('source_channel') or 'band hai')}</b>\n\n"
        "Ek private channel jisme tum deals daalo — main wahan se khud utha ke "
        "post channel pe bhej dunga. Mujhe DM karne ki zaroorat nahi.\n\n"
        "<b>Kaise jode:</b> mujhe us channel mein admin banao, main pooch lunga."
    )


def source_kb(cfg: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("✏️ Draft channel badlo", callback_data="set_source_edit")]]
    if cfg.get("source_channel"):
        rows.append([InlineKeyboardButton("🗑️ Draft channel hatao", callback_data="set_source_off")])
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


def silent_text(silent: bool) -> str:
    if silent:
        return ("🔔 <b>Notification</b>\n\nStatus: <b>🔕 SILENT</b>\n\n"
                "Post channel mein normal aati hai, par subscribers ke phone pe "
                "awaaz nahi hoti.")
    return "🔔 <b>Notification</b>\n\nStatus: <b>🔔 LOUD</b>\n\nHar post pe poori notification jaati hai."


def silent_kb(silent: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Loud karo" if silent else "🔕 Silent karo",
                              callback_data="silent_toggle")],
        BACK_ROW,
    ])


def wm_text(wm: dict) -> str:
    return (
        f"🖼️ <b>Watermark</b>\n\n"
        f"Status : <b>{'✅ ON' if wm.get('enabled') else '❌ OFF'}</b>\n"
        f"Text   : <code>{esc(wm.get('text') or 'set nahi')}</code>\n\n"
        f"<i>Product photo ke neeche right corner pe tumhara naam lagta hai "
        f"(jaise @MyDeals), taaki koi photo copy kare to bhi tumhara naam dikhe.</i>"
    )


def wm_kb(wm: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Text badlo", callback_data="wm_set_text")],
        [InlineKeyboardButton("🔴 Band karo" if wm.get("enabled") else "🟢 Chalu karo",
                              callback_data="wm_toggle")],
        BACK_ROW,
    ])


def amz_text(cfg: dict) -> str:
    detailed = cfg.get("amz_detailed", True)
    f        = cfg.get("amz_fields", {})
    if detailed:
        on_list = [FIELD_LABELS.get(k, k) for k in FIELD_ORDER if f.get(k)]
        body = (f"Mode: <b>✅ DETAILED</b>\n"
                f"🖼️ Image {'ON' if f.get('image') else 'OFF'}   "
                f"🔗 Link {'ON' if f.get('link', True) else 'OFF'}\n\n"
                f"<b>Post mein dikhega:</b> {esc(', '.join(on_list)) or '—'}")
    else:
        body = "Mode: <b>❌ MINIMAL</b>\n\nSirf <b>Price + Link</b> jaayega. Koi photo, koi detail nahi."
    return ("🛍️ <b>Amazon Post Details</b>\n\n" + body +
            "\n\n<i>Jis cheez ko post mein nahi dikhana, uspe tap karke ❌ kar do.</i>")


def amz_kb(cfg: dict) -> InlineKeyboardMarkup:
    detailed = cfg.get("amz_detailed", True)
    f        = cfg.get("amz_fields", {})
    rows = [[InlineKeyboardButton("🔻 MINIMAL karo (price + link)" if detailed else "🔺 DETAILED karo",
                                  callback_data="amz_mode")]]
    if detailed:
        pair = []
        for k in ["image", "link"] + FIELD_ORDER:
            pair.append(InlineKeyboardButton(f"{_onoff(f.get(k))} {FIELD_LABELS.get(k, k)}",
                                             callback_data=f"amzf_{k}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


def park_text(cfg: dict, amz_n: int, oth_n: int) -> str:
    if cfg.get("park_post"):
        body = (f"Status: <b>🅿️ PARK ON</b>\n\n"
                f"Draft channel ki deals jama hoti hain aur har ghante ke shuru mein "
                f"(agla: <b>{next_batch_label()}</b>) ek saath post hoti hain — sabse "
                f"zyada discount wali pehle.\n\n"
                f"Queue mein: <b>{amz_n}</b> Amazon + <b>{oth_n}</b> other\n"
                f"<i>{QUEUE_MAX_AGE_HOURS} ghante se purani deal apne aap hat jaati hai. "
                f"Mujhe DM ki hui deal hamesha turant jaati hai.</i>")
    else:
        body = "Status: <b>⚡ INSTANT</b>\n\nDeal aate hi turant post ho jaati hai."
    return "🅿️ <b>Park Post</b>\n\n" + body


def park_kb(cfg: dict, pending: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("⚡ INSTANT karo" if cfg.get("park_post") else "🅿️ PARK karo",
                                  callback_data="park_toggle")]]
    if pending:
        rows.append([InlineKeyboardButton(f"📤 Abhi bhej do ({pending})", callback_data="park_flush")])
        rows.append([InlineKeyboardButton(f"🗑️ Queue khali karo ({pending})", callback_data="park_clear")])
    rows.append(BACK_ROW)
    return InlineKeyboardMarkup(rows)


def hf_text(kind: str, d: dict) -> str:
    name = "Header" if kind == "header" else "Footer"
    spot = "har post ke sabse upar" if kind == "header" else "har post ke sabse neeche"
    return (f"{'🔝' if kind == 'header' else '🔚'} <b>{name}</b>\n\n"
            f"Status : {_onoff(d.get('enabled'))}\n"
            f"Text   : <code>{esc(d.get('text') or '—')}</code>\n\n"
            f"<i>Ye line {spot} lagti hai.</i>")


def hf_kb(kind: str, d: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Text badlo", callback_data=f"hf_{kind}_text")],
        [InlineKeyboardButton("🔴 Band karo" if d.get("enabled") else "🟢 Chalu karo",
                              callback_data=f"hf_{kind}_toggle")],
        BACK_ROW,
    ])


def sb_text(btns: dict) -> str:
    b1, b2 = btns.get("btn1", {}), btns.get("btn2", {})
    buy, cart = btns.get("buy", {}), btns.get("cart", {})
    link_note = "post se link hat ke button mein aa jaata hai" if buy.get("enabled") \
        else "link post ke text mein rehta hai"
    return (
        "🎛️ <b>Post ke neeche Buttons</b>\n\n"
        f"⚡ <b>Buy Now</b> — {_onoff(buy.get('enabled'))}  ({esc(buy.get('label', '-'))})\n"
        f"   <i>ON karne pe {link_note}.</i>\n\n"
        f"🛒 <b>Add to Cart</b> — {_onoff(cart.get('enabled'))}  ({esc(cart.get('label', '-'))})\n"
        f"   <i>Cart mein jaane se kamai ka time 24 ghante se 89 din ho jaata hai.</i>\n\n"
        f"📌 <b>Button 1</b> — {_onoff(b1.get('enabled'))}  {esc(b1.get('label', '-'))}\n"
        f"   Link: <code>{esc(b1.get('url') or '—')}</code>\n"
        f"📌 <b>Button 2</b> — {_onoff(b2.get('enabled'))}  {esc(b2.get('label', '-'))}\n"
        f"   Link: <code>{esc(b2.get('url') or '—')}</code>\n\n"
        f"<i>Buy Now aur Cart sirf Amazon post pe lagte hain, tumhare tag ke saath.</i>"
    )


def sb_main_kb(btns: dict) -> InlineKeyboardMarkup:
    buy, cart = btns.get("buy", {}), btns.get("cart", {})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_onoff(buy.get('enabled'))} Buy Now", callback_data="sb_buy"),
         InlineKeyboardButton(f"{_onoff(cart.get('enabled'))} Add to Cart", callback_data="sb_cart")],
        [InlineKeyboardButton(f"✏️ {btns.get('btn1', {}).get('label', 'Button 1')}",
                              callback_data="sb_btn1"),
         InlineKeyboardButton(f"✏️ {btns.get('btn2', {}).get('label', 'Button 2')}",
                              callback_data="sb_btn2")],
        BACK_ROW,
    ])


def amz_btn_text(key: str, b: dict) -> str:
    name = "⚡ Buy Now" if key == "buy" else "🛒 Add to Cart"
    extra = ("<b>ON</b> → link post ke text se hat ke is button mein aa jaata hai."
             if key == "buy" else "Product seedha customer ke cart mein jaata hai.")
    return (f"<b>{name} Button</b>\n\n"
            f"Naam   : <b>{esc(b.get('label', '-'))}</b>\n"
            f"Status : {_onoff(b.get('enabled'))}\n\n"
            f"<i>Link main khud banata hoon tumhare tag se. {extra}</i>")


def amz_btn_kb(key: str, b: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Naam badlo", callback_data=f"sb_{key}_rename")],
        [InlineKeyboardButton("🔴 Band karo" if b.get("enabled") else "🟢 Chalu karo",
                              callback_data=f"sb_{key}_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sb_main")],
    ])


def btn_text(key: str, btn: dict) -> str:
    return (f"🎛️ <b>Button {key[-1]}</b>\n\n"
            f"📝 Naam  : <b>{esc(btn.get('label', '-'))}</b>\n"
            f"🔗 Link  : <code>{esc(btn.get('url') or 'set nahi')}</code>\n"
            f"Status : {_onoff(btn.get('enabled'))}\n\n"
            f"<i>Jaise 'Join Channel' button jo tumhare channel pe le jaaye.</i>")


def btn_kb(key: str, btn: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Naam badlo", callback_data=f"sb_{key}_rename"),
         InlineKeyboardButton("🔗 Link daalo", callback_data=f"sb_{key}_link")],
        [InlineKeyboardButton("🔴 Band karo" if btn.get("enabled") else "🟢 Chalu karo",
                              callback_data=f"sb_{key}_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sb_main")],
    ])


def search_text(cfg: dict) -> str:
    on = cfg.get("search_links")
    return ("🔍 <b>Search Links</b>\n\n"
            f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
            "Amazon search page / deals page wale link (product nahi, list wale).\n\n"
            "<b>ON</b> → ye bhi post honge, tumhare tag ke saath\n"
            "<b>OFF</b> → chhod diye jaate hain (default)")


def pdauto_text(cfg: dict) -> str:
    on = cfg.get("pricedrop_autopost")
    return ("📉 <b>Price Drop Auto-Post</b>\n\n"
            f"Status: <b>{'✅ ON' if on else '❌ OFF'}</b>\n\n"
            "Jo products tum /track se track kar rahe ho, unka price gire to:\n\n"
            "<b>ON</b> → main seedha channel pe post kar dunga + tumhe bata dunga\n"
            "<b>OFF</b> → sirf tumhe message aayega, post karna hai ya nahi tum chuno")


def toggle_kb(cb: str, on: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Band karo" if on else "🟢 Chalu karo", callback_data=cb)],
        BACK_ROW,
    ])


def status_text(uid: int, cfg: dict) -> str:
    amz_n, oth_n = queue_counts(uid)
    f, btns = cfg.get("amz_fields", {}), cfg.get("buttons", {})
    wm, hdr, ftr = cfg.get("watermark", {}), cfg.get("header", {}), cfg.get("footer", {})
    on_fields = [FIELD_LABELS.get(k, k) for k in FIELD_ORDER if f.get(k)]
    lines = [
        "📊 <b>Tumhari Settings</b>\n",
        f"🏷️ Tag     : <code>{esc(cfg.get('tag') or '❌ /tag')}</code>",
        f"📢 Channel : <b>{esc(cfg.get('channel_title') or cfg.get('channel') or '❌ /channel')}</b>",
        f"📥 Draft   : <b>{esc(cfg.get('source_title') or cfg.get('source_channel') or 'band')}</b>\n",
        f"🅿️ Mode    : <b>{'PARK (har ghante)' if cfg.get('park_post') else 'INSTANT'}</b>"
        + (f" — agli batch <b>{next_batch_label()}</b>" if cfg.get('park_post') else ""),
        f"📋 Queue   : {amz_n} Amazon + {oth_n} other",
        f"🕐 Time    : <b>{hhmm(now_local())}</b> ({TZ_NAME})",
        f"🔔 Notify  : {'🔕 Silent' if cfg.get('silent') else '🔔 Loud'}",
        f"🛍️ Amazon  : <b>{'DETAILED' if cfg.get('amz_detailed') else 'MINIMAL (price+link)'}</b>",
        f"🖼️ Image   : {_onoff(f.get('image'))}   Watermark: {_onoff(wm.get('enabled'))}",
        f"🔝 Header  : {_onoff(hdr.get('enabled'))}   🔚 Footer: {_onoff(ftr.get('enabled'))}",
        f"🔍 Search links : {_onoff(cfg.get('search_links'))}",
        f"📉 Price drop auto-post : {_onoff(cfg.get('pricedrop_autopost'))}\n",
        f"⚡ Buy Now : {_onoff(btns.get('buy', {}).get('enabled'))}   "
        f"🛒 Cart: {_onoff(btns.get('cart', {}).get('enabled'))}",
        f"📌 Button 1: {_onoff(btns.get('btn1', {}).get('enabled'))}   "
        f"📌 Button 2: {_onoff(btns.get('btn2', {}).get('enabled'))}\n",
        f"<b>Post mein:</b> {esc(', '.join(on_fields)) or '—'}",
    ]
    return "\n".join(lines)


# =============================================================================
# COMMANDS (gate main.py mein lagta hai — yahan uid already verified hai)
# =============================================================================
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data.pop("action", None)
    cfg = load_config(uid)
    await update.message.reply_text(settings_home_text(cfg), parse_mode=ParseMode.HTML,
                                    reply_markup=settings_home_kb(cfg))


async def cmd_status(update, context, uid):
    await update.message.reply_text(status_text(uid, load_config(uid)), parse_mode=ParseMode.HTML)


async def cmd_tag(update, context, uid):
    args = context.args or []
    if args:
        await _apply_tag(update.message.reply_text, uid, args[0])
        return
    await update.message.reply_text(tag_text(load_config(uid)), parse_mode=ParseMode.HTML,
                                    reply_markup=tag_kb(), disable_web_page_preview=True)


async def _apply_tag(reply, uid: int, raw: str) -> bool:
    tag = (raw or "").strip()
    if not is_valid_tag(tag):
        await reply("⚠️ Ye tag sahi nahi lag raha.\n\n"
                    "Amazon India tag aisa hota hai: <code>mydeals-21</code>\n"
                    "(aakhir mein <b>-21</b> hota hai). Dobara bhejo.",
                    parse_mode=ParseMode.HTML)
        return False
    cfg = load_config(uid)
    cfg["tag"] = tag
    ok = save_config(uid, cfg)
    await reply(f"{'✅' if ok else '❌'} <b>Affiliate tag {'save ho gaya' if ok else 'save nahi hua'}!</b>\n"
                f"🏷️ <code>{esc(tag)}</code>\n\nAb har link mein yahi tag lagega.",
                parse_mode=ParseMode.HTML)
    return ok


async def cmd_channel(update, context, uid):
    args = context.args or []
    if args:
        ident = normalize_channel_ident(args[0])
        msg = (await save_post_channel(context.bot, uid, ident)) if ident else \
            "⚠️ Channel samajh nahi aaya — @username bhejo."
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(channel_text(load_config(uid)), parse_mode=ParseMode.HTML,
                                    reply_markup=channel_kb())


async def cmd_source(update, context, uid):
    args = context.args or []
    cfg = load_config(uid)
    if args and args[0].lower() in ("off", "band", "clear", "remove", "hatao"):
        cfg["source_channel"], cfg["source_title"] = "", ""
        save_config(uid, cfg)
        await update.message.reply_text("❌ <b>Draft channel hata diya.</b>\nAb sirf DM wala tareeka chalega.",
                                        parse_mode=ParseMode.HTML)
        return
    if args:
        ident = normalize_channel_ident(args[0])
        msg = (await save_source_channel(context.bot, uid, ident)) if ident else \
            "⚠️ Channel samajh nahi aaya — @username bhejo."
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(source_text(cfg), parse_mode=ParseMode.HTML,
                                    reply_markup=source_kb(cfg))


async def cmd_amz_post(update, context, uid):
    cfg  = load_config(uid)
    args = context.args or []
    if args:
        a = args[0].lower()
        if a in ("on", "detailed", "full", "off", "minimal", "min"):
            cfg["amz_detailed"] = a in ("on", "detailed", "full")
            save_config(uid, cfg)
            await update.message.reply_text(
                "✅ Amazon post <b>DETAILED</b> mode." if cfg["amz_detailed"]
                else "✅ Amazon post <b>MINIMAL</b> mode — sirf price + link.",
                parse_mode=ParseMode.HTML)
            return
    await update.message.reply_text(amz_text(cfg), parse_mode=ParseMode.HTML, reply_markup=amz_kb(cfg))


async def cmd_park_post(update, context, uid):
    cfg  = load_config(uid)
    args = context.args or []
    if args:
        a = args[0].lower()
        if a in ("on", "chalu"):
            cfg["park_post"] = True
            save_config(uid, cfg)
            await update.message.reply_text(f"🅿️ <b>Park mode ON.</b>\nAgli batch <b>{next_batch_label()}</b> baje.",
                                            parse_mode=ParseMode.HTML)
            return
        if a in ("off", "band"):
            cfg["park_post"] = False
            save_config(uid, cfg)
            await update.message.reply_text("⚡ <b>Instant mode ON.</b>\nQueue mein padi posts abhi bhej raha hoon...",
                                            parse_mode=ParseMode.HTML)
            context.application.create_task(flush_queue(context.application, uid, reason="park off"))
            return
    amz_n, oth_n = queue_counts(uid)
    await update.message.reply_text(park_text(cfg, amz_n, oth_n), parse_mode=ParseMode.HTML,
                                    reply_markup=park_kb(cfg, amz_n + oth_n))


async def cmd_queue(update, context, uid):
    cfg   = load_config(uid)
    items = queue_fetch_all(uid)
    if not items:
        await update.message.reply_text(
            f"📋 <b>Queue khali hai.</b>\n\nMode: <b>{'PARK (har ghante)' if cfg.get('park_post') else 'INSTANT'}</b>",
            parse_mode=ParseMode.HTML)
        return
    amz = [i for i in items if i["kind"] == "amazon"]
    oldest = min(i["arrived_at"] for i in items)
    age = int((utcnow() - oldest).total_seconds() // 60)
    await update.message.reply_text(
        f"📋 <b>Queue — {len(items)} posts</b>\n\n"
        f"🛍️ Amazon : <b>{len(amz)}</b>\n📝 Other  : <b>{len(items) - len(amz)}</b>\n"
        f"⏱️ Sabse purani: {age} minute\n\n"
        f"<i>Post karte waqt price fresh laaya jayega, khatam deals hata di jaayengi.</i>",
        parse_mode=ParseMode.HTML, reply_markup=park_kb(cfg, len(items)))


async def cmd_silent(update, context, uid):
    cfg  = load_config(uid)
    args = context.args or []
    if args and args[0].lower() in ("on", "chalu", "off", "band"):
        cfg["silent"] = args[0].lower() in ("on", "chalu")
        save_config(uid, cfg)
        await update.message.reply_text(f"{'🔕' if cfg['silent'] else '🔔'} Silent posting "
                                        f"<b>{'ON' if cfg['silent'] else 'OFF'}</b>.",
                                        parse_mode=ParseMode.HTML)
        return
    s = cfg.get("silent", True)
    await update.message.reply_text(silent_text(s), parse_mode=ParseMode.HTML, reply_markup=silent_kb(s))


async def cmd_header(update, context, uid):
    d = load_config(uid).get("header", {})
    await update.message.reply_text(hf_text("header", d), parse_mode=ParseMode.HTML, reply_markup=hf_kb("header", d))


async def cmd_footer(update, context, uid):
    d = load_config(uid).get("footer", {})
    await update.message.reply_text(hf_text("footer", d), parse_mode=ParseMode.HTML, reply_markup=hf_kb("footer", d))


async def cmd_watermark(update, context, uid):
    cfg  = load_config(uid)
    wm   = cfg.setdefault("watermark", {"enabled": False, "text": ""})
    args = context.args or []
    if args and args[0].lower() in ("on", "off"):
        if args[0].lower() == "on" and not (wm.get("text") or "").strip():
            await update.message.reply_text("⚠️ Pehle watermark ka text set karo.",
                                            reply_markup=wm_kb(wm))
            return
        wm["enabled"] = args[0].lower() == "on"
        save_config(uid, cfg)
        await update.message.reply_text(f"✅ Watermark <b>{'ON' if wm['enabled'] else 'OFF'}</b>.",
                                        parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(wm_text(wm), parse_mode=ParseMode.HTML, reply_markup=wm_kb(wm))


async def cmd_setbutton(update, context, uid):
    btns = load_config(uid).get("buttons", {})
    await update.message.reply_text(sb_text(btns), parse_mode=ParseMode.HTML, reply_markup=sb_main_kb(btns))


async def cmd_exportconfig(update, context, uid):
    data = json.dumps(load_config(uid), indent=2, ensure_ascii=False)
    if len(data) > 3500:
        data = data[:3500] + "\n... (kata gaya)"
    await update.message.reply_text(f"📦 <b>Settings Backup</b>\n\n<pre>{esc(data)}</pre>",
                                    parse_mode=ParseMode.HTML)


# =============================================================================
# CALLBACKS — True return = handle ho gaya
# =============================================================================
async def handle_settings_callback(query, context, uid: int, data: str) -> bool:
    async def show(text, kb=None):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)
        except Exception:
            pass

    def ask(action: str, **extra):
        context.user_data["action"] = action
        for k, v in extra.items():
            context.user_data[k] = v

    # ── Settings home + sections ──────────────────────────────────────────
    if data == "set_home":
        context.user_data.pop("action", None)
        cfg = load_config(uid)
        await show(settings_home_text(cfg), settings_home_kb(cfg))
        return True
    if data == "set_tag":
        await show(tag_text(load_config(uid)), tag_kb())
        return True
    if data == "set_tag_edit":
        ask("wait_tag")
        await show("🏷️ Apna Amazon affiliate tag bhejo (jaise <code>mydeals-21</code>):")
        return True
    if data == "set_channel":
        await show(channel_text(load_config(uid)), channel_kb())
        return True
    if data == "set_channel_edit":
        ask("wait_channel_id")
        await show("📢 Channel ka <b>@username</b> ya <b>ID</b> bhejo, ya channel ka koi message "
                   "<b>forward</b> kar do.\n\n<i>Pehle mujhe us channel mein admin bana dena.</i>")
        return True
    if data == "set_source":
        cfg = load_config(uid)
        await show(source_text(cfg), source_kb(cfg))
        return True
    if data == "set_source_edit":
        ask("wait_source_id")
        await show("📥 Draft channel ka <b>@username</b> ya <b>ID</b> bhejo, ya uska koi message "
                   "<b>forward</b> kar do.\n\n<i>Pehle mujhe us channel mein admin bana dena.</i>")
        return True
    if data == "set_source_off":
        cfg = load_config(uid)
        cfg["source_channel"], cfg["source_title"] = "", ""
        save_config(uid, cfg)
        await show("❌ <b>Draft channel hata diya.</b>", source_kb(cfg))
        return True
    if data == "set_amz":
        cfg = load_config(uid)
        await show(amz_text(cfg), amz_kb(cfg))
        return True
    if data == "set_wm":
        wm = load_config(uid).get("watermark", {})
        await show(wm_text(wm), wm_kb(wm))
        return True
    if data in ("set_header", "set_footer"):
        kind = data.split("_")[1]
        d = load_config(uid).get(kind, {})
        await show(hf_text(kind, d), hf_kb(kind, d))
        return True
    if data == "set_silent":
        s = load_config(uid).get("silent", True)
        await show(silent_text(s), silent_kb(s))
        return True
    if data == "set_park":
        cfg = load_config(uid)
        amz_n, oth_n = queue_counts(uid)
        await show(park_text(cfg, amz_n, oth_n), park_kb(cfg, amz_n + oth_n))
        return True
    if data in ("set_search", "set_pdauto"):
        cfg = load_config(uid)
        key = "search_links" if data == "set_search" else "pricedrop_autopost"
        txt = search_text if data == "set_search" else pdauto_text
        await show(txt(cfg), toggle_kb(data + "_t", cfg.get(key)))
        return True
    if data in ("set_search_t", "set_pdauto_t"):
        cfg = load_config(uid)
        key = "search_links" if data == "set_search_t" else "pricedrop_autopost"
        txt = search_text if data == "set_search_t" else pdauto_text
        cfg[key] = not cfg.get(key, False)
        save_config(uid, cfg)
        await show(txt(cfg), toggle_kb(data, cfg[key]))
        return True

    # ── Silent ────────────────────────────────────────────────────────────
    if data == "silent_toggle":
        cfg = load_config(uid)
        cfg["silent"] = not cfg.get("silent", True)
        save_config(uid, cfg)
        await show(silent_text(cfg["silent"]), silent_kb(cfg["silent"]))
        return True

    # ── Amazon fields ─────────────────────────────────────────────────────
    if data == "amz_mode":
        cfg = load_config(uid)
        cfg["amz_detailed"] = not cfg.get("amz_detailed", True)
        save_config(uid, cfg)
        await show(amz_text(cfg), amz_kb(cfg))
        return True
    if data.startswith("amzf_"):
        key = data.split("_", 1)[1]
        if key not in FIELD_LABELS:
            return True
        cfg = load_config(uid)
        f = cfg.setdefault("amz_fields", {})
        f[key] = not f.get(key, False)
        save_config(uid, cfg)
        await show(amz_text(cfg), amz_kb(cfg))
        return True

    # ── Park ──────────────────────────────────────────────────────────────
    if data == "park_toggle":
        cfg = load_config(uid)
        cfg["park_post"] = not cfg.get("park_post", False)
        save_config(uid, cfg)
        amz_n, oth_n = queue_counts(uid)
        await show(park_text(cfg, amz_n, oth_n), park_kb(cfg, amz_n + oth_n))
        if not cfg["park_post"] and (amz_n + oth_n):
            context.application.create_task(flush_queue(context.application, uid, reason="park off"))
        return True
    if data == "park_flush":
        amz_n, oth_n = queue_counts(uid)
        total = amz_n + oth_n
        if not total:
            await show("📋 Queue khali hai.")
            return True
        mins = int((total * POST_GAP_SECONDS) // 60) + 1
        await show(f"📤 <b>{total} post bhej raha hoon</b> — ~{mins} minute lagega.\n\n"
                   f"<i>Telegram ki limit ki wajah se beech mein gap rakhna padta hai. "
                   f"Ho jaane pe summary bhej dunga.</i>")
        context.application.create_task(flush_queue(context.application, uid, reason="manual"))
        return True
    if data == "park_clear":
        n = queue_clear(uid)
        await show(f"🗑️ <b>{n} post queue se hata di.</b>", park_kb(load_config(uid), 0))
        return True

    # ── Header / Footer ───────────────────────────────────────────────────
    if data.startswith("hf_") and data.count("_") == 2:
        _, kind, act = data.split("_", 2)
        if kind not in ("header", "footer"):
            return True
        cfg = load_config(uid)
        d = cfg.setdefault(kind, {})
        if act == "toggle":
            if not d.get("enabled") and not (d.get("text") or "").strip():
                ask(f"hf_wait_{kind}")
                await show(f"✏️ Pehle <b>{kind.title()}</b> ka text bhejo (max 120 character):")
                return True
            d["enabled"] = not d.get("enabled", False)
            save_config(uid, cfg)
            await show(hf_text(kind, d), hf_kb(kind, d))
        elif act == "text":
            ask(f"hf_wait_{kind}")
            await show(f"✏️ <b>{kind.title()} ka naya text</b> bhejo (max 120 character).\n\n"
                       f"<i>Hatana hai to <code>-</code> bhej do.</i>")
        return True
    if data == "hf_confirm":
        kind = context.user_data.pop("hf_kind", "header")
        val  = context.user_data.pop("hf_pending", None)
        context.user_data.pop("action", None)
        cfg = load_config(uid)
        d = cfg.setdefault(kind, {})
        if val is not None:
            d["text"] = val
            d["enabled"] = bool(val)
            save_config(uid, cfg)
        await show(hf_text(kind, d), hf_kb(kind, d))
        return True

    # ── Watermark ─────────────────────────────────────────────────────────
    if data == "wm_toggle":
        cfg = load_config(uid)
        wm = cfg.setdefault("watermark", {"enabled": False, "text": ""})
        if not wm.get("enabled") and not (wm.get("text") or "").strip():
            ask("wm_wait_text")
            await show("✏️ Pehle watermark ka text bhejo (jaise @MyDeals, max 30 character):")
            return True
        wm["enabled"] = not wm.get("enabled", False)
        save_config(uid, cfg)
        await show(wm_text(wm), wm_kb(wm))
        return True
    if data == "wm_set_text":
        ask("wm_wait_text")
        await show("✏️ Naya watermark text bhejo (jaise @MyDeals, max 30 character):")
        return True
    if data == "wm_confirm_text":
        new_text = context.user_data.pop("wm_pending_text", None)
        context.user_data.pop("action", None)
        cfg = load_config(uid)
        wm = cfg.setdefault("watermark", {})
        if new_text:
            wm["text"] = new_text
            wm["enabled"] = True
            save_config(uid, cfg)
        await show(wm_text(wm), wm_kb(wm))
        return True

    # ── Buttons ───────────────────────────────────────────────────────────
    if data == "sb_main":
        btns = load_config(uid).get("buttons", {})
        await show(sb_text(btns), sb_main_kb(btns))
        return True
    if data in ("sb_buy", "sb_cart"):
        key = data[3:]
        b = load_config(uid).get("buttons", {}).get(key, {})
        await show(amz_btn_text(key, b), amz_btn_kb(key, b))
        return True
    if data in ("sb_btn1", "sb_btn2"):
        key = data[3:]
        b = load_config(uid).get("buttons", {}).get(key, {})
        await show(btn_text(key, b), btn_kb(key, b))
        return True
    if data.startswith("sb_") and data.endswith("_toggle"):
        key = data[3:-7]
        if key not in ("buy", "cart", "btn1", "btn2"):
            return True
        cfg = load_config(uid)
        b = cfg.setdefault("buttons", {}).setdefault(key, {})
        if key in ("btn1", "btn2") and not b.get("enabled") and not b.get("url"):
            ask("sb_wait_link", sb_key=key)
            await show(f"🔗 Pehle Button {key[-1]} ka link bhejo (https:// ya t.me/ se shuru):")
            return True
        b["enabled"] = not b.get("enabled", False)
        save_config(uid, cfg)
        if key in ("buy", "cart"):
            await show(amz_btn_text(key, b), amz_btn_kb(key, b))
        else:
            await show(btn_text(key, b), btn_kb(key, b))
        return True
    if data.startswith("sb_") and data.endswith("_rename"):
        key = data[3:-7]
        if key not in ("buy", "cart", "btn1", "btn2"):
            return True
        ask("sb_wait_label", sb_key=key)
        await show("📝 Button ka naya naam bhejo (max 20 character):")
        return True
    if data.startswith("sb_") and data.endswith("_link"):
        key = data[3:-5]
        if key not in ("btn1", "btn2"):
            return True
        ask("sb_wait_link", sb_key=key)
        await show(f"🔗 Button {key[-1]} ka link bhejo (https:// ya t.me/ se shuru):")
        return True
    if data == "sb_confirm":
        key   = context.user_data.pop("sb_key", "btn1")
        val   = context.user_data.pop("sb_pending", None)
        field = context.user_data.pop("sb_field", None)
        context.user_data.pop("action", None)
        cfg = load_config(uid)
        b = cfg.setdefault("buttons", {}).setdefault(key, {})
        if val and field:
            b[field] = val
            if field == "url":
                b["enabled"] = True
            save_config(uid, cfg)
        if key in ("buy", "cart"):
            await show("✅ <b>Saved!</b>\n\n" + amz_btn_text(key, b), amz_btn_kb(key, b))
        else:
            await show("✅ <b>Saved!</b>\n\n" + btn_text(key, b), btn_kb(key, b))
        return True

    return False


# =============================================================================
# TEXT INPUT — True return = handle ho gaya
# =============================================================================
def _save_cancel(cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Save", callback_data=cb),
                                  InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])


async def handle_settings_input(update: Update, context, uid: int, action: str) -> bool:
    msg   = update.message
    text  = (msg.text or "").strip()
    reply = msg.reply_text

    if action == "wait_tag":
        if await _apply_tag(reply, uid, text):
            context.user_data.pop("action", None)
        return True

    if action in ("wait_channel_id", "wait_source_id"):
        ident = channel_from_message(msg) or normalize_channel_ident(text)
        if not ident:
            await reply("⚠️ Channel samajh nahi aaya.\n\n"
                        "Channel ka <b>@username</b> bhejo, ya channel ka koi message "
                        "<b>forward</b> karo.\n<i>Private channel ho to forward wala tareeka "
                        "use karo (invite link se nahi hota).</i>", parse_mode=ParseMode.HTML)
            return True
        if action == "wait_channel_id":
            out = await save_post_channel(context.bot, uid, ident)
        else:
            out = await save_source_channel(context.bot, uid, ident)
        if out.startswith("✅"):
            context.user_data.pop("action", None)
        await reply(out, parse_mode=ParseMode.HTML)
        return True

    if action == "wm_wait_text":
        if not text or len(text) > 30:
            await reply("⚠️ 1 se 30 character ke beech bhejo.")
            return True
        context.user_data["wm_pending_text"] = text
        context.user_data["action"] = None
        await reply(f"📋 Preview: <code>{esc(text)}</code>\n\nSave karein?",
                    parse_mode=ParseMode.HTML, reply_markup=_save_cancel("wm_confirm_text"))
        return True

    if action.startswith("hf_wait_"):
        kind = action.replace("hf_wait_", "")
        if len(text) > 120:
            await reply("⚠️ Max 120 character.")
            return True
        val = "" if text == "-" else text
        context.user_data.update(hf_pending=val, hf_kind=kind, action=None)
        prev = esc(val) if val else "<i>(khali — band ho jayega)</i>"
        await reply(f"📋 <b>{kind.title()} preview:</b>\n\n{prev}\n\nSave karein?",
                    parse_mode=ParseMode.HTML, reply_markup=_save_cancel("hf_confirm"))
        return True

    if action == "sb_wait_label":
        if not text or len(text) > 20:
            await reply("⚠️ Naam 1 se 20 character ka ho.")
            return True
        context.user_data.update(sb_pending=text, sb_field="label", action=None)
        await reply(f"📋 Preview: <b>{esc(text)}</b>\n\nSave karein?",
                    parse_mode=ParseMode.HTML, reply_markup=_save_cancel("sb_confirm"))
        return True

    if action == "sb_wait_link":
        if text.lower().startswith(("t.me/", "telegram.me/", "www.")):
            text = "https://" + text
        elif text.startswith("@") and len(text) > 4:
            text = "https://t.me/" + text[1:]
        if not _BTN_URL_RE.match(text) or len(text) > 500:
            await reply("⚠️ Ye link sahi nahi lag raha.\n"
                        "Aise bhejo: <code>https://t.me/mychannel</code> ya <code>@mychannel</code>",
                        parse_mode=ParseMode.HTML)
            return True
        context.user_data.update(sb_pending=text, sb_field="url", action=None)
        await reply(f"📋 Preview: <code>{esc(text)}</code>\n\nSave karein?",
                    parse_mode=ParseMode.HTML, reply_markup=_save_cancel("sb_confirm"))
        return True

    return False

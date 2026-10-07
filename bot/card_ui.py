"""
card_ui.py — har TASK ka apna Image Card design. Pro / Premium plan mein
milta hai (Basic pe 🔒). Toggle, picker, Preview, Default pe wapas.

Callback:  t:<tid>:card                     card home
           t:<tid>:card:t:<toggle>
           t:<tid>:card:p:<key>             picker
           t:<tid>:card:s:<key>:<value>
           t:<tid>:card:prev | reset | reset_ok
"""
import asyncio
import io
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode

from amazon_api import get_products_by_asins
from card import (
    CARD_CHOICES, CARD_TOGGLES, DEFAULT_CARD, SAMPLE_PRODUCT,
    clean_card, render_card, placeholder_photo,
)
from database import last_amazon_asins
from storage import save_task
from ui import btn, tr, track, GREEN, BLUE, RED
from users import limits

logger = logging.getLogger(__name__)
esc = html_lib.escape

TOGGLE_LABELS = {
    "show_price":    ("Offer Price", "Offer Price"),
    "show_discount": ("Discount", "Discount"),
    "show_mrp":      ("MRP", "MRP"),
    "show_rating":   ("Rating", "Rating"),
}

PICKER_TITLES = {
    "layout":         ("📐 Card size", "📐 Card ka size"),
    "image_size":     ("🖼️ Product photo size", "🖼️ Product photo kitni badi"),
    "image_side":     ("↔️ Photo side", "↔️ Photo kis taraf"),
    "theme":          ("🎨 Background theme", "🎨 Background theme"),
    "font":           ("🔤 Font", "🔤 Font"),
    "price_shape":    ("✴️ Offer Price badge shape", "✴️ Offer Price badge ka shape"),
    "price_color":    ("🖍️ Offer Price colour", "🖍️ Offer Price ka rang"),
    "discount_color": ("🔴 Discount circle colour", "🔴 Discount gole ka rang"),
}

_preview_busy: set = set()


def _l(pair, lang):
    return pair[0] if lang == "en" else pair[1]


def _choice(key, card):
    return CARD_CHOICES[key].get(card.get(key), "—")


def home_text(task, lang) -> str:
    c = clean_card(task["cfg"].get("card"))
    name = esc(task["cfg"].get("name") or f"Task #{task['id']}")
    status = (tr(lang, "✅ <b>ON</b> — every Amazon post gets this card",
                 "✅ <b>ON</b> — har Amazon post pe ye card banega") if c["enabled"]
              else tr(lang, "❌ <b>OFF</b> — the plain Amazon photo is posted",
                      "❌ <b>OFF</b> — Amazon ki seedhi photo jaati hai"))
    return tr(
        lang,
        f"🎨 <b>Image Card</b> — {name}\n\nStatus: {status}\n\n"
        "A clean picture: product photo on one side, Offer Price, Discount and MRP on the other.\n\n"
        f"📐 Size: <b>{_choice('layout', c)}</b>   🖼️ Photo: <b>{_choice('image_size', c)}</b>, {_choice('image_side', c)}\n"
        f"🎨 Theme: <b>{_choice('theme', c)}</b>   🔤 Font: <b>{_choice('font', c)}</b>\n\n"
        "<i>Change anything, then tap 👁️ Preview once. After that every post uses this design "
        "automatically. The watermark comes from the 💧 Watermark setting.</i>",
        f"🎨 <b>Image Card</b> — {name}\n\nStatus: {status}\n\n"
        "Ek saaf photo: ek taraf product, doosri taraf Offer Price, Discount aur MRP.\n\n"
        f"📐 Size: <b>{_choice('layout', c)}</b>   🖼️ Photo: <b>{_choice('image_size', c)}</b>, {_choice('image_side', c)}\n"
        f"🎨 Theme: <b>{_choice('theme', c)}</b>   🔤 Font: <b>{_choice('font', c)}</b>\n\n"
        "<i>Kuch bhi badlein, phir 👁️ Preview ek baar dekh lein. Uske baad har post pe yahi design "
        "apne aap lagega. Watermark 💧 Watermark setting se aata hai.</i>",
    )


def home_kb(task, lang) -> InlineKeyboardMarkup:
    tid = task["id"]
    c = clean_card(task["cfg"].get("card"))
    base = f"t:{tid}:card"

    def t(key):
        return btn(f"{'✅' if c[key] else '❌'} {_l(TOGGLE_LABELS[key], lang)}", callback_data=f"{base}:t:{key}")

    def p(key, label):
        return btn(label, callback_data=f"{base}:p:{key}")

    return InlineKeyboardMarkup([
        [btn(tr(lang, "🔴 Turn Card OFF", "🔴 Card band karein") if c["enabled"]
             else tr(lang, "🟢 Turn Card ON", "🟢 Card chalu karein"),
             RED if c["enabled"] else GREEN, callback_data=f"{base}:t:enabled")],
        [btn(tr(lang, "👁️ Preview", "👁️ Preview dekhein"), BLUE, callback_data=f"{base}:prev")],
        [t("show_price"), t("show_discount")],
        [t("show_mrp"), t("show_rating")],
        [p("layout", "📐 Size"), p("image_size", tr(lang, "🖼️ Photo Size", "🖼️ Photo Size"))],
        [p("image_side", tr(lang, "↔️ Photo Side", "↔️ Photo Side")), p("theme", "🎨 Theme")],
        [p("font", "🔤 Font"), p("price_shape", tr(lang, "✴️ Badge Shape", "✴️ Badge Shape"))],
        [p("price_color", tr(lang, "🖍️ Price Colour", "🖍️ Price ka Rang")),
         p("discount_color", tr(lang, "🔴 Discount Colour", "🔴 Discount ka Rang"))],
        [btn("💧 Watermark", callback_data=f"t:{tid}:wm"),
         btn(tr(lang, "↩️ Reset", "↩️ Default"), callback_data=f"{base}:reset")],
        [btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"t:{tid}"),
         btn(tr(lang, "📋 All Tasks", "📋 Saare Tasks"), callback_data="tl")],
    ])


def picker_kb(task, key, lang) -> InlineKeyboardMarkup:
    tid = task["id"]
    current = clean_card(task["cfg"].get("card")).get(key)
    rows, pair = [], []
    for val, label in CARD_CHOICES[key].items():
        cur = val == current
        pair.append(btn(("✔️ " if cur else "") + label, GREEN if cur else "",
                        callback_data=f"t:{tid}:card:s:{key}:{val}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"t:{tid}:card")])
    return InlineKeyboardMarkup(rows)


def locked_text(lang) -> str:
    return tr(lang,
              "🔒 <b>Image Card is a Pro feature</b>\n\n"
              "Turn every Amazon deal into a clean picture — product photo with Offer Price, "
              "Discount and MRP, in your colours and font.\n\n"
              "Available on 🥈 <b>Pro</b> and 🥇 <b>Premium</b>.",
              "🔒 <b>Image Card Pro feature hai</b>\n\n"
              "Har Amazon deal ko ek saaf photo mein badlein — product photo ke saath Offer Price, "
              "Discount aur MRP, aapke rang aur font mein.\n\n"
              "🥈 <b>Pro</b> aur 🥇 <b>Premium</b> plan mein milta hai.")


async def _preview_product(uid: int):
    from engine import _download_image   # circular import se bachne ke liye
    try:
        asins = last_amazon_asins(uid, 5)
        if asins:
            got = await get_products_by_asins(asins[:3])
            for a in asins[:3]:
                p = got.get(a)
                if p and p.get("image_url") and p.get("deal_price"):
                    raw = await _download_image(p["image_url"])
                    if raw:
                        return raw, p
    except Exception as e:
        logger.error(f"Preview product fail: {e}")
    return placeholder_photo(), dict(SAMPLE_PRODUCT)


async def send_preview(query, context, uid, task, lang):
    if uid in _preview_busy:
        return
    _preview_busy.add(uid)
    try:
        cfg = task["cfg"]
        raw, product = await _preview_product(uid)
        card_cfg = dict(clean_card(cfg.get("card")), enabled=True)
        out = await asyncio.to_thread(render_card, raw, product, card_cfg, cfg.get("watermark", {}),
                                      bool(cfg.get("amazon_badge", True)))
        if not out:
            await query.answer(tr(lang, "❌ Preview failed, try again.", "❌ Preview nahi bana, dobara try karein."),
                               show_alert=True)
            return
        note = "" if clean_card(cfg.get("card"))["enabled"] else tr(
            lang, "\n⚠️ <i>The card is OFF — turn it ON to use it in posts.</i>",
            "\n⚠️ <i>Card abhi OFF hai — posts mein lagane ke liye ON karein.</i>")
        m = await query.message.reply_photo(
            InputFile(io.BytesIO(out), filename="preview.jpg"),
            caption=tr(lang, "👁️ <b>Preview</b> — your posts will look like this.",
                       "👁️ <b>Preview</b> — aapki posts aisi dikhengi.") + note,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn(tr(lang, "⬅️ Card Settings", "⬅️ Card Settings"),
                                                    callback_data=f"t:{task['id']}:card")]]))
        track(context, m)
    finally:
        _preview_busy.discard(uid)


async def handle(query, context, uid, task, parts, lang):
    """parts = callback ke 'card' ke baad wale hisse."""
    from task_ui import show, upgrade_kb

    tid = task["id"]
    if not limits(uid)["card"]:
        kb = upgrade_kb(lang)
        kb = InlineKeyboardMarkup(list(kb.inline_keyboard) +
                                  [[btn(tr(lang, "⬅️ Back", "⬅️ Wapas"), callback_data=f"t:{tid}")]])
        await show(query, context, locked_text(lang), kb)
        return

    cfg = task["cfg"]
    card = clean_card(cfg.get("card"))
    act = parts[0] if parts else ""

    def save():
        cfg["card"] = card
        save_task(uid, tid, cfg)

    if act == "t" and len(parts) > 1 and parts[1] in CARD_TOGGLES:
        card[parts[1]] = not card[parts[1]]
        save()
    elif act == "p" and len(parts) > 1 and parts[1] in PICKER_TITLES:
        await show(query, context, _l(PICKER_TITLES[parts[1]], lang) + "\n\n"
                   + tr(lang, "Pick one 👇", "Jo pasand ho wo chunein 👇"), picker_kb(task, parts[1], lang))
        return
    elif act == "s" and len(parts) > 2 and parts[1] in CARD_CHOICES and parts[2] in CARD_CHOICES[parts[1]]:
        card[parts[1]] = parts[2]
        save()
    elif act == "prev":
        await send_preview(query, context, uid, task, lang)
        return
    elif act == "reset":
        await show(query, context,
                   tr(lang, "↩️ <b>Reset the card design to default?</b>\n<i>Card ON/OFF stays as it is.</i>",
                      "↩️ <b>Card ka design default pe wapas karein?</b>\n<i>Card ON/OFF waisa hi rahega.</i>"),
                   InlineKeyboardMarkup([[btn(tr(lang, "✅ Yes, reset", "✅ Haan, reset"), RED,
                                              callback_data=f"t:{tid}:card:reset_ok"),
                                          btn(tr(lang, "❌ No", "❌ Nahi"), callback_data=f"t:{tid}:card")]]))
        return
    elif act == "reset_ok":
        card = dict(DEFAULT_CARD, enabled=card["enabled"])
        save()

    await show(query, context, home_text(task, lang), home_kb(task, lang))

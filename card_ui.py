"""
card_ui.py — Image Card ki settings (har user ki apni). Toggle, picker,
Preview aur Default pe wapas. Sab user_config (DB) mein save hota hai.

Callback format:  card_home | card_t:<toggle> | card_p:<key> | card_s:<key>:<value>
                  card_prev | card_reset | card_reset_ok
"""
import asyncio
import io
import logging
import html as html_lib

from telegram import InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode

from amazon_api import get_products_by_asins
from card import (
    CARD_CHOICES, CARD_TOGGLES, DEFAULT_CARD, WM_POSITIONS, SAMPLE_PRODUCT,
    clean_card, render_card, placeholder_photo,
)
from database import last_amazon_asins
from storage import load_config, save_config
from ui import btn, GREEN, BLUE, RED

logger = logging.getLogger(__name__)
esc = html_lib.escape

TOGGLE_LABELS = {
    "enabled":       "Image Card",
    "show_price":    "Offer Price",
    "show_discount": "Discount",
    "show_mrp":      "MRP",
    "show_rating":   "Rating",
}

PICKER_TITLES = {
    "layout":         "📐 Card ka size",
    "image_size":     "🖼️ Product photo kitni badi",
    "image_side":     "↔️ Photo kis taraf",
    "theme":          "🎨 Background theme",
    "font":           "🔤 Font",
    "price_shape":    "✴️ Offer Price badge ka shape",
    "price_color":    "🖍️ Offer Price ka rang",
    "discount_color": "🔴 Discount gole ka rang",
    "wm_position":    "📍 Watermark kahan lage",
}

_preview_busy: set = set()


def _choice(key: str, card: dict) -> str:
    return CARD_CHOICES[key].get(card.get(key), "—")


def _on(v) -> str:
    return "✅" if v else "❌"


def card_home_text(cfg: dict) -> str:
    c = clean_card(cfg.get("card"))
    wm = cfg.get("watermark", {})
    wm_line = (f"✅ <code>{esc(wm.get('text'))}</code> — {WM_POSITIONS.get(wm.get('position'), '')}"
               if wm.get("enabled") and (wm.get("text") or "").strip() else "❌ band")
    status = ("✅ <b>ON</b> — har Amazon post pe card banega" if c["enabled"]
              else "❌ <b>OFF</b> — Amazon ki seedhi photo jaati hai")
    return (
        "🎨 <b>Image Card</b>\n\n"
        f"Status: {status}\n\n"
        "White background pe ek taraf product photo, doosri taraf Offer Price, "
        "Discount, MRP — sab tumhari pasand se.\n\n"
        f"📐 Size: <b>{_choice('layout', c)}</b>   🖼️ Photo: <b>{_choice('image_size', c)}</b>, "
        f"{_choice('image_side', c)}\n"
        f"🎨 Theme: <b>{_choice('theme', c)}</b>   🔤 Font: <b>{_choice('font', c)}</b>\n"
        f"💧 Watermark: {wm_line}\n\n"
        "<i>Kuch bhi badlo, phir 👁️ Preview se ek baar dekh lo. Uske baad har post "
        "pe yahi design apne aap lagega.</i>"
    )


def card_home_kb(cfg: dict) -> InlineKeyboardMarkup:
    c = clean_card(cfg.get("card"))
    wm = cfg.get("watermark", {})
    wm_on = wm.get("enabled") and (wm.get("text") or "").strip()

    def t(key):
        return btn(f"{_on(c[key])} {TOGGLE_LABELS[key]}", callback_data=f"card_t:{key}")

    def p(key, label):
        return btn(label, callback_data=f"card_p:{key}")

    return InlineKeyboardMarkup([
        [btn("🔴 Card band karo" if c["enabled"] else "🟢 Card chalu karo",
             RED if c["enabled"] else GREEN, callback_data="card_t:enabled")],
        [btn("👁️ Preview dekho", BLUE, callback_data="card_prev")],
        [t("show_price"), t("show_discount")],
        [t("show_mrp"), t("show_rating")],
        [p("layout", "📐 Size"), p("image_size", "🖼️ Photo size")],
        [p("image_side", "↔️ Photo side"), p("theme", "🎨 Theme")],
        [p("font", "🔤 Font"), p("price_shape", "✴️ Badge shape")],
        [p("price_color", "🖍️ Price rang"), p("discount_color", "🔴 Discount rang")],
        [btn(f"{'✅' if wm_on else '❌'} Watermark", callback_data="set_wm"),
         p("wm_position", "📍 Watermark jagah")],
        [btn("↩️ Default pe wapas", callback_data="card_reset")],
        [btn("⬅️ Settings", callback_data="set_home"), btn("❌ Close", callback_data="cancel")],
    ])


def picker_kb(key: str, cfg: dict) -> InlineKeyboardMarkup:
    if key == "wm_position":
        options, current = WM_POSITIONS, cfg.get("watermark", {}).get("position", "bottom_right")
    else:
        options, current = CARD_CHOICES[key], clean_card(cfg.get("card")).get(key)
    rows, pair = [], []
    for val, label in options.items():
        pair.append(btn(("✔️ " if val == current else "") + label,
                        GREEN if val == current else "",
                        callback_data=f"card_s:{key}:{val}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("⬅️ Back", callback_data="card_home")])
    return InlineKeyboardMarkup(rows)


async def _preview_product(uid: int):
    """User ki latest Amazon post ka product — na mile to sample."""
    from engine import _download_image   # circular import se bachne ke liye yahan
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


async def send_preview(query, uid: int):
    if uid in _preview_busy:
        return
    _preview_busy.add(uid)
    try:
        cfg = load_config(uid)
        raw, product = await _preview_product(uid)
        card_cfg = dict(clean_card(cfg.get("card")), enabled=True)
        out = await asyncio.to_thread(render_card, raw, product, card_cfg, cfg.get("watermark", {}))
        if not out:
            await query.message.reply_text("❌ Preview nahi ban paya, dobara try karo.")
            return
        note = ("" if clean_card(cfg.get("card"))["enabled"]
                else "\n⚠️ <i>Card abhi OFF hai — posts mein lagane ke liye ON karo.</i>")
        await query.message.reply_photo(
            InputFile(io.BytesIO(out), filename="preview.jpg"),
            caption=f"👁️ <b>Preview</b> — tumhari posts aisi dikhengi.{note}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[btn("⬅️ Card settings", callback_data="card_home")]]))
    finally:
        _preview_busy.discard(uid)


async def handle_card_callback(query, context, uid: int, data: str) -> bool:
    """True = handle ho gaya."""
    if not (data.startswith("card_") or data == "card_home"):
        return False

    async def show(text, kb):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                          disable_web_page_preview=True)
        except Exception as e:
            if "not modified" in str(e).lower():
                return
            # Preview photo pe "Back" dabaya — photo edit nahi hoti, naya message bhejo
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                           disable_web_page_preview=True)

    cfg = load_config(uid)

    if data == "card_home":
        await show(card_home_text(cfg), card_home_kb(cfg))
        return True

    if data.startswith("card_t:"):
        key = data.split(":", 1)[1]
        if key in CARD_TOGGLES:
            card = clean_card(cfg.get("card"))
            card[key] = not card[key]
            cfg["card"] = card
            save_config(uid, cfg)
        await show(card_home_text(cfg), card_home_kb(cfg))
        return True

    if data.startswith("card_p:"):
        key = data.split(":", 1)[1]
        if key in PICKER_TITLES:
            await show(f"{PICKER_TITLES[key]}\n\nJo pasand ho wo chuno 👇", picker_kb(key, cfg))
        return True

    if data.startswith("card_s:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, key, val = parts
            if key == "wm_position" and val in WM_POSITIONS:
                cfg.setdefault("watermark", {})["position"] = val
                save_config(uid, cfg)
            elif key in CARD_CHOICES and val in CARD_CHOICES[key]:
                card = clean_card(cfg.get("card"))
                card[key] = val
                cfg["card"] = card
                save_config(uid, cfg)
        await show(card_home_text(cfg), card_home_kb(cfg))
        return True

    if data == "card_prev":
        await send_preview(query, uid)
        return True

    if data == "card_reset":
        await show("↩️ <b>Card ki saari design settings default pe wapas kar dein?</b>\n\n"
                   "<i>Card ON/OFF aur watermark text waise hi rahenge.</i>",
                   InlineKeyboardMarkup([[btn("✅ Haan, reset karo", RED, callback_data="card_reset_ok"),
                                          btn("❌ Nahi", callback_data="card_home")]]))
        return True

    if data == "card_reset_ok":
        enabled = clean_card(cfg.get("card"))["enabled"]
        cfg["card"] = dict(DEFAULT_CARD, enabled=enabled)
        cfg.setdefault("watermark", {})["position"] = "bottom_right"
        save_config(uid, cfg)
        await show("✅ <b>Default pe wapas kar diya.</b>\n\n" + card_home_text(cfg), card_home_kb(cfg))
        return True

    return False

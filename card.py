"""
card.py — deal image card. White (ya theme) background, ek taraf product photo,
doosri taraf Offer Price badge, Discount gola, MRP aur Rating. Har cheez user
ki apni setting se on/off aur style hoti hai. Watermark bhi yahin lagta hai.
"""
import io
import os
import math
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# key: (naam, bold file, semibold file)
FONTS = {
    "poppins":    ("Poppins",    "Poppins-Bold.ttf",     "Poppins-SemiBold.ttf"),
    "montserrat": ("Montserrat", "Montserrat-Bold.ttf",  "Montserrat-SemiBold.ttf"),
    "roboto":     ("Roboto",     "Roboto-Bold.ttf",      "Roboto-Medium.ttf"),
    "bebas":      ("Bebas Neue", "BebasNeue-Regular.ttf", "BebasNeue-Regular.ttf"),
}
FALLBACK_FONT = "poppins"      # Hindi wagaira isme dikh jaata hai

LAYOUTS = {
    "square":   ("Square 1:1",    (1080, 1080)),
    "wide":     ("Wide 16:9",     (1280, 720)),
    "portrait": ("Portrait 4:5",  (1080, 1350)),
}

IMAGE_SIZES = {"s": ("Small", 0.46), "m": ("Medium", 0.56), "l": ("Large", 0.66)}
IMAGE_SIDES = {"right": "Right ➡️", "left": "⬅️ Left"}

# key: (naam, background, text, halka text, photo ke peeche panel?)
THEMES = {
    "white":  ("⚪ White",      (255, 255, 255), (20, 20, 20),    (120, 120, 120), False),
    "grey":   ("🩶 Light Grey", (238, 240, 243), (20, 20, 20),    (110, 110, 110), True),
    "yellow": ("🟡 Yellow",     (255, 214, 10),  (20, 20, 20),    (90, 70, 0),     True),
    "dark":   ("⚫ Dark",       (24, 26, 31),    (245, 245, 245), (170, 170, 170), True),
}

COLORS = {
    "red":    ("🔴 Red",    (229, 40, 40)),
    "green":  ("🟢 Green",  (30, 140, 60)),
    "blue":   ("🔵 Blue",   (30, 99, 214)),
    "orange": ("🟠 Orange", (245, 124, 0)),
    "purple": ("🟣 Purple", (123, 31, 162)),
    "teal":   ("🩵 Teal",   (38, 166, 154)),
    "pink":   ("🩷 Pink",   (216, 27, 96)),
    "black":  ("⚫ Black",  (30, 30, 30)),
}

PRICE_SHAPES = {
    "star":   "✴️ Zig-zag",
    "circle": "⚪ Gola",
    "box":    "▭ Box",
    "ribbon": "🎀 Ribbon",
}

WM_POSITIONS = {
    "top":          "⬆️ Upar beech",
    "bottom_right": "↘️ Neeche right",
    "bottom_left":  "↙️ Neeche left",
    "center":       "⏺️ Beech mein",
}

DEFAULT_CARD = {
    "enabled":        False,
    "layout":         "square",
    "image_side":     "right",
    "image_size":     "m",
    "theme":          "white",
    "font":           "poppins",
    "show_price":     True,
    "price_shape":    "star",
    "price_color":    "teal",
    "show_discount":  True,
    "discount_color": "red",
    "show_mrp":       True,
    "show_rating":    False,
}

# Picker wale options — settings UI yahin se list banata hai
CARD_CHOICES = {
    "layout":         {k: v[0] for k, v in LAYOUTS.items()},
    "image_side":     IMAGE_SIDES,
    "image_size":     {k: v[0] for k, v in IMAGE_SIZES.items()},
    "theme":          {k: v[0] for k, v in THEMES.items()},
    "font":           {k: v[0] for k, v in FONTS.items()},
    "price_shape":    PRICE_SHAPES,
    "price_color":    {k: v[0] for k, v in COLORS.items()},
    "discount_color": {k: v[0] for k, v in COLORS.items()},
}
CARD_TOGGLES = ("enabled", "show_price", "show_discount", "show_mrp", "show_rating")


def clean_card(card) -> dict:
    """Missing / galat value ko default se bharo."""
    out = dict(DEFAULT_CARD)
    if isinstance(card, dict):
        for k, v in card.items():
            if k in CARD_TOGGLES:
                out[k] = bool(v)
            elif k in CARD_CHOICES and v in CARD_CHOICES[k]:
                out[k] = v
    return out


# =============================================================================
# FONT HELPERS
# =============================================================================
_font_cache: dict = {}


def _font(key: str, size: int, bold: bool = True):
    key = key if key in FONTS else FALLBACK_FONT
    size = max(8, int(size))
    ck = (key, size, bold)
    f = _font_cache.get(ck)
    if f is None:
        path = os.path.join(FONT_DIR, FONTS[key][1] if bold else FONTS[key][2])
        try:
            f = ImageFont.truetype(path, size)
        except Exception:
            try:
                f = ImageFont.load_default(size=size)
            except Exception:
                f = ImageFont.load_default()
        if len(_font_cache) > 400:
            _font_cache.clear()
        _font_cache[ck] = f
    return f


def _render_bytes(font, ch: str) -> bytes:
    im = Image.new("L", (64, 64))
    ImageDraw.Draw(im).text((8, 8), ch, font=font, fill=255)
    return im.tobytes()


_glyph_cache: dict = {}


def _font_for_text(key: str, text: str) -> str:
    """Font mein sab akshar nahi hain (jaise Hindi) to fallback font."""
    key = key if key in FONTS else FALLBACK_FONT
    if key == FALLBACK_FONT:
        return key
    probe = _font(key, 30)
    blank = _glyph_cache.get((key, None))
    if blank is None:
        blank = _glyph_cache[(key, None)] = _render_bytes(probe, "￿")
    for ch in set(text):
        if ch.isspace() or ord(ch) < 128:
            continue
        ok = _glyph_cache.get((key, ch))
        if ok is None:
            ok = _glyph_cache[(key, ch)] = _render_bytes(probe, ch) != blank
        if not ok:
            return FALLBACK_FONT
    return key


def _text_w(draw, text, font) -> int:
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0]


def _fit(draw, key, text, max_w, start, minimum=12, bold=True):
    """Text max_w mein aa jaaye itna bada font."""
    key = _font_for_text(key, text)
    size = int(start)
    while size > minimum:
        f = _font(key, size, bold)
        if _text_w(draw, text, f) <= max_w:
            return f
        size -= max(1, size // 12)
    return _font(key, minimum, bold)


# =============================================================================
# SHAPES
# =============================================================================
def _darker(c, k=0.72):
    return tuple(max(0, int(x * k)) for x in c[:3])


def _starburst(cx, cy, r_out, r_in, points=22):
    pts = []
    for i in range(points * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.pi * i / points - math.pi / 2
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _star(cx, cy, r):
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.45
        a = math.pi * i / 5 - math.pi / 2
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def _draw_price_badge(img, cx, cy, w, h, card, fonts_key, price, theme):
    """Offer Price badge — 4 shape. Returns nothing, img pe seedha banata hai."""
    d = ImageDraw.Draw(img)
    color = COLORS[card["price_color"]][1]
    bg, fg = theme[1], theme[2]
    shape = card["price_shape"]
    label = "Offer Price"

    if shape == "star":
        r = min(w, h) / 2
        d.polygon(_starburst(cx, cy, r, r * 0.86), fill=bg, outline=color, width=max(4, int(r * 0.05)))
        text_c, inner_w = fg, r * 1.25
    elif shape == "circle":
        r = min(w, h) / 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline=_darker(color), width=max(4, int(r * 0.06)))
        text_c, inner_w = (255, 255, 255), r * 1.45
    elif shape == "box":
        bw, bh = w, h * 0.62
        d.rounded_rectangle([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2],
                            radius=int(bh * 0.18), fill=color)
        text_c, inner_w = (255, 255, 255), bw * 0.86
    else:  # ribbon
        bw, bh = w, h * 0.56
        n = bh * 0.32
        x1, x2, y1, y2 = cx - bw / 2, cx + bw / 2, cy - bh / 2, cy + bh / 2
        tail = _darker(color)
        d.polygon([(x1 - n * 0.2, y1 + n), (x1 + n, y1 + n), (x1 + n, y2 + n * 0.6),
                   (x1 - n * 0.2, y2 + n * 0.6), (x1 + n * 0.5, (y1 + y2) / 2 + n * 0.8)], fill=tail)
        d.polygon([(x2 + n * 0.2, y1 + n), (x2 - n, y1 + n), (x2 - n, y2 + n * 0.6),
                   (x2 + n * 0.2, y2 + n * 0.6), (x2 - n * 0.5, (y1 + y2) / 2 + n * 0.8)], fill=tail)
        d.rectangle([x1 + n * 0.45, y1, x2 - n * 0.45, y2], fill=color)
        text_c, inner_w = (255, 255, 255), (bw - n) * 0.88

    big = _fit(d, fonts_key, price, inner_w, h * 0.30)
    small = _fit(d, fonts_key, label, inner_w * 0.85, h * 0.13, bold=False)
    gap = h * 0.05
    sh = d.textbbox((0, 0), label, font=small, anchor="ls")
    bh_ = d.textbbox((0, 0), price, font=big, anchor="ls")
    small_h, big_h = sh[3] - sh[1], bh_[3] - bh_[1]
    top = cy - (small_h + gap + big_h) / 2
    d.text((cx, top + small_h), label, font=small, fill=text_c, anchor="ms")
    d.text((cx, top + small_h + gap + big_h), price, font=big, fill=text_c, anchor="ms")


def _draw_discount(img, cx, cy, r, card, fonts_key, pct):
    d = ImageDraw.Draw(img)
    color = COLORS[card["discount_color"]][1]
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_darker(color, 0.6))
    rr = r * 0.9
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=color)
    top = f"{pct}%"
    f1 = _fit(d, fonts_key, top, rr * 1.45, r * 0.62)
    f2 = _fit(d, fonts_key, "off", rr * 1.2, r * 0.48)
    d.text((cx, cy + r * 0.02), top, font=f1, fill=(255, 255, 255), anchor="ms")
    d.text((cx, cy + r * 0.08), "off", font=f2, fill=(255, 255, 255), anchor="mt")


def _draw_mrp(img, cx, cy, max_w, size, fonts_key, mrp, theme):
    d = ImageDraw.Draw(img)
    txt = f"MRP {mrp}"
    f = _fit(d, fonts_key, txt, max_w, size, bold=False)
    d.text((cx, cy), txt, font=f, fill=theme[3], anchor="mm")
    b = d.textbbox((cx, cy), txt, font=f, anchor="mm")
    # sirf price wale hisse pe line
    pre = _text_w(d, "MRP ", f)
    y = (b[1] + b[3]) / 2 + 1
    d.line([(b[0] + pre - 4, y), (b[2] + 4, y)], fill=theme[3], width=max(2, int(size * 0.09)))


def _draw_rating(img, cx, cy, max_w, size, fonts_key, rating, theme):
    d = ImageDraw.Draw(img)
    try:
        val = max(0.0, min(5.0, float(rating)))
    except (TypeError, ValueError):
        return
    txt = f"{val:.1f}"
    f = _fit(d, fonts_key, txt, max_w * 0.3, size)
    r = size * 0.42
    star_w = r * 2.1
    total = star_w * 5 + size * 0.3 + _text_w(d, txt, f)
    if total > max_w:
        k = max_w / total
        r, star_w, total = r * k, star_w * k, max_w
    x = cx - total / 2
    gold, empty = (255, 179, 0), (205, 205, 205)
    for i in range(5):
        sx = x + star_w * i + star_w / 2
        d.polygon(_star(sx, cy, r), fill=empty)
        fill = max(0.0, min(1.0, val - i))
        if fill > 0:
            layer = Image.new("L", img.size, 0)
            ImageDraw.Draw(layer).polygon(_star(sx, cy, r), fill=255)
            cut = Image.new("L", img.size, 0)
            ImageDraw.Draw(cut).rectangle([sx - r, cy - r, sx - r + 2 * r * fill, cy + r], fill=255)
            from PIL import ImageChops
            img.paste(gold, mask=ImageChops.multiply(layer, cut))
    d.text((x + star_w * 5 + size * 0.3, cy), txt, font=f, fill=theme[2], anchor="lm")


# =============================================================================
# WATERMARK (card aur normal photo dono ke liye)
# =============================================================================
def draw_watermark(img: Image.Image, text: str, position: str = "bottom_right",
                   font_key: str = "poppins", dark_text: bool = True) -> Image.Image:
    """
    'top' → photo ke upar beech mein saaf text (jaise "Posted On ...").
    Baaki → semi-transparent kaala box + safed text.
    img RGBA hona chahiye. Naya RGBA image wapas.
    """
    text = (text or "").strip()
    if not text:
        return img
    W, H = img.size
    position = position if position in WM_POSITIONS else "bottom_right"
    key = _font_for_text(font_key, text)
    probe = ImageDraw.Draw(img)

    if position == "top":
        size = max(18, min(46, int(W * 0.032)))
        f = _fit(probe, key, text, W * 0.9, size)
        probe.text((W / 2, max(10, H * 0.022)), text, font=f,
                   fill=(25, 25, 25) if dark_text else (245, 245, 245), anchor="mt")
        return img

    size = max(18, min(40, int(W * 0.035)))
    f = _fit(probe, key, text, W * 0.6, size)
    b = probe.textbbox((0, 0), text, font=f)
    tw, th = b[2] - b[0], b[3] - b[1]
    pad_x, pad_y, margin = int(size * 0.5), int(size * 0.3), int(W * 0.018) + 4
    bw, bh = tw + pad_x * 2, th + pad_y * 2
    if position == "bottom_left":
        x1, y1 = margin, H - bh - margin
    elif position == "center":
        x1, y1 = (W - bw) // 2, (H - bh) // 2
    else:
        x1, y1 = W - bw - margin, H - bh - margin

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle([x1, y1, x1 + bw, y1 + bh],
                                              radius=int(size * 0.3), fill=(0, 0, 0, 165))
    img = Image.alpha_composite(img, overlay)
    ImageDraw.Draw(img).text((x1 + pad_x - b[0], y1 + pad_y - b[1]), text, font=f,
                             fill=(255, 255, 255, 255))
    return img


# =============================================================================
# CARD
# =============================================================================
def _trim_white(im: Image.Image) -> Image.Image:
    """Amazon photo ke charon taraf ki faltu safed jagah hatao."""
    try:
        rgb = im.convert("RGB")
        bg = Image.new("RGB", rgb.size, (255, 255, 255))
        from PIL import ImageChops
        diff = ImageChops.difference(rgb, bg).convert("L").point(lambda p: 255 if p > 18 else 0)
        box = diff.getbbox()
        if box and (box[2] - box[0]) > 20 and (box[3] - box[1]) > 20:
            return im.crop(box)
    except Exception:
        pass
    return im


def render_card(photo_bytes: bytes, product: dict, card: dict, wm: dict = None) -> bytes | None:
    """
    Card banao. photo_bytes = Amazon ki product photo.
    wm = {"enabled", "text", "position"} (user ki watermark setting).
    Returns JPEG bytes, ya None (kuch gadbad — caller normal photo bheje).
    """
    try:
        card = clean_card(card)
        W, H = LAYOUTS[card["layout"]][1]
        theme = THEMES[card["theme"]]
        fk = card["font"]

        img = Image.new("RGBA", (W, H), theme[1] + (255,))
        d = ImageDraw.Draw(img)

        wm = wm or {}
        wm_text = (wm.get("text") or "").strip() if wm.get("enabled") else ""
        wm_pos = wm.get("position") or "bottom_right"
        top_pad = int(H * 0.075) if (wm_text and wm_pos == "top") else int(H * 0.04)
        margin = int(min(W, H) * 0.04)

        # ── Info column me kya-kya hai ────────────────────────────────────
        price = (product.get("deal_price") or "").strip()
        mrp = (product.get("actual_price") or "").strip()
        try:
            pct = int(product.get("discount_pct") or 0)
        except (TypeError, ValueError):
            pct = 0
        rating = (product.get("rating") or "").strip()

        items = []
        if card["show_price"] and price:
            items.append("price")
        if card["show_discount"] and pct > 0:
            items.append("discount")
        if card["show_mrp"] and mrp and mrp != price:
            items.append("mrp")
        if card["show_rating"] and rating:
            items.append("rating")

        frac = IMAGE_SIZES[card["image_size"]][1] if items else 0.92
        img_w = int(W * frac)
        col_w = W - img_w
        right = card["image_side"] == "right"
        img_x0 = col_w if right else 0
        col_x0 = 0 if right else img_w

        # ── Product photo ─────────────────────────────────────────────────
        photo = Image.open(io.BytesIO(photo_bytes))
        photo = _trim_white(photo.convert("RGBA"))
        box_w = img_w - margin * (2 if items else 2)
        box_h = H - top_pad - margin
        if theme[4]:
            pad = int(margin * 0.6)
            panel = [img_x0 + margin * 0.5, top_pad, img_x0 + img_w - margin * 0.5, H - margin]
            d.rounded_rectangle(panel, radius=int(min(W, H) * 0.03), fill=(255, 255, 255))
            box_w -= pad * 2
            box_h -= pad * 2
        scale = min(box_w / photo.width, box_h / photo.height)
        nw, nh = max(1, int(photo.width * scale)), max(1, int(photo.height * scale))
        photo = photo.resize((nw, nh), Image.LANCZOS)
        px = int(img_x0 + (img_w - nw) / 2)
        py = int(top_pad + (H - top_pad - margin - nh) / 2)
        img.alpha_composite(photo, (px, py))

        # ── Info column ───────────────────────────────────────────────────
        if items:
            cx = col_x0 + col_w / 2
            inner = col_w - margin * 1.2
            avail = H - top_pad - margin
            want = {
                "price":    inner * 0.95,
                "discount": inner * 0.62,
                "mrp":      inner * 0.16,
                "rating":   inner * 0.15,
            }
            gap = inner * 0.07
            total = sum(want[i] for i in items) + gap * (len(items) - 1)
            k = min(1.0, avail / total) if total else 1.0
            y = top_pad + (avail - total * k) / 2
            for it in items:
                h = want[it] * k
                c = y + h / 2
                if it == "price":
                    _draw_price_badge(img, cx, c, min(inner, h * 1.1), h, card, fk, price, theme)
                elif it == "discount":
                    _draw_discount(img, cx, c, h / 2, card, fk, pct)
                elif it == "mrp":
                    _draw_mrp(img, cx, c, inner, h * 0.85, fk, mrp, theme)
                elif it == "rating":
                    _draw_rating(img, cx, c, inner, h, fk, rating, theme)
                y += h + gap * k

        if wm_text:
            img = draw_watermark(img, wm_text, wm_pos, fk, dark_text=card["theme"] != "dark")

        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        logger.error(f"Card render fail: {e}")
        return None


def placeholder_photo() -> bytes:
    """Preview ke liye — jab koi asli product photo na mile."""
    im = Image.new("RGB", (800, 800), (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([170, 120, 630, 680], radius=40, fill=(225, 232, 240), outline=(170, 180, 195), width=6)
    d.ellipse([300, 230, 500, 430], fill=(190, 200, 215))
    f = _font("poppins", 54)
    d.text((400, 560), "Product", font=f, fill=(120, 130, 145), anchor="mm")
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


SAMPLE_PRODUCT = {
    "title": "Sample Product",
    "deal_price": "₹135", "actual_price": "₹280", "discount_pct": 52, "rating": "4.3",
}

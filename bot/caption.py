"""
caption.py — Amazon deal caption builder. Har field config se on/off hota hai,
aur caption Telegram ki limit ke andar rehta hai.
"""
import re
import html as html_lib

from amazon_api import display_link

_TAG_RE = re.compile(r"<[^>]+>")

# Telegram limits
PHOTO_CAPTION_LIMIT = 1024
TEXT_LIMIT          = 4096

# Caption mein fields kis order se bharenge
FIELD_ORDER = [
    "title", "deal", "mrp", "price", "savings", "discount",
    "rating", "reviews", "stock", "brand", "seller", "rank", "features",
]

FIELD_LABELS = {
    "title":    "Title",
    "deal":     "Deal badge",
    "mrp":      "MRP",
    "price":    "Buy At",
    "savings":  "You Save",
    "discount": "Discount",
    "rating":   "Rating",
    "reviews":  "Reviews",
    "stock":    "Stock",
    "brand":    "Brand",
    "seller":   "Seller",
    "rank":     "Best Seller Rank",
    "features": "Features",
    "image":    "Image",
    "link":     "Link",
}


def _u16(text: str) -> int:
    """Telegram length UTF-16 mein ginta hai — emoji = 2."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _visible_len(html_text: str) -> int:
    return _u16(html_lib.unescape(_TAG_RE.sub("", html_text)))


_PARTIAL_ENTITY_RE = re.compile(r"&[A-Za-z0-9#]*$")


def _cut(plain_html: str, n: int) -> str:
    """Escaped text ko n tak kaato — '&amp;' jaisa code beech se na kate."""
    return _PARTIAL_ENTITY_RE.sub("", plain_html[:max(0, n)])


def _safe_truncate(html_text: str, max_visible: int = PHOTO_CAPTION_LIMIT - 4) -> str:
    """Non-Amazon post limit se lamba ho to kaato (formatting hata ke, link wali last line bacha ke)."""
    if _visible_len(html_text) <= max_visible:
        return html_text
    lines = html_text.rsplit("\n", 1)
    if len(lines) == 2:
        body, last_line = lines
        last_visible = _visible_len(last_line) + 1
        body_limit   = max_visible - last_visible - 3
        body_plain   = _TAG_RE.sub("", body)
        if body_limit > 20 and last_visible < max_visible // 2:
            return _cut(body_plain, body_limit) + "...\n" + last_line
    plain = _TAG_RE.sub("", html_text)
    return _cut(plain, max_visible - 3) + "..."


def _short_title(title: str, words: int = 10) -> str:
    if not title:
        return "Hot Deal"
    parts = title.split()
    if len(parts) <= words:
        return title
    return " ".join(parts[:words]) + "..."


def _esc(v) -> str:
    return html_lib.escape(str(v or ""))


# =============================================================================
# FIELD RENDERERS — har ek ek line (ya None) deta hai
# =============================================================================
def _render_field(name: str, p: dict) -> str | None:
    if name == "title":
        t = (p.get("title") or "").strip()
        return f"🔥 <b>{_esc(_short_title(t))}</b>" if t else None

    if name == "deal":
        badge = (p.get("deal_badge") or "").strip()
        ends  = (p.get("deal_ends") or "").strip()
        if not badge:
            return None
        if ends:
            return f"⚡ <b>{_esc(badge)}</b> — {_esc(ends)}"
        return f"⚡ <b>{_esc(badge)}</b>"

    if name == "mrp":
        mrp   = (p.get("actual_price") or "").strip()
        price = (p.get("deal_price") or "").strip()
        if mrp and price and mrp != price:
            return f"❌ MRP:          <b><s>{_esc(mrp)}</s></b>"
        return None

    if name == "price":
        price = (p.get("deal_price") or "").strip()
        return f"🏷️ Buy At:        <b>{_esc(price)}</b>" if price else None

    if name == "savings":
        s = (p.get("savings") or "").strip()
        return f"💵 You Save:     <b>{_esc(s)}</b>" if s else None

    if name == "discount":
        try:
            d = int(p.get("discount_pct") or 0)
        except (ValueError, TypeError):
            d = 0
        return f"📉 Discount:      <b>{d}% OFF</b>" if d > 0 else None

    if name == "rating":
        r = (p.get("rating") or "").strip()
        return f"⭐ Rating:        <b>{_esc(r)}/5</b>" if r else None

    if name == "reviews":
        rc = (p.get("review_count") or "").strip()
        return f"👥 Reviews:       <b>{_esc(rc)}</b>" if rc else None

    if name == "stock":
        st = (p.get("stock_note") or "").strip()
        return f"📦 Stock:         <b>{_esc(st)}</b>" if st else None

    if name == "brand":
        b = (p.get("brand") or "").strip()
        return f"🏷️ Brand:         <b>{_esc(b)}</b>" if b else None

    if name == "seller":
        m = (p.get("seller") or "").strip()
        return f"🏪 Seller:        <b>{_esc(m)}</b>" if m else None

    if name == "rank":
        rk = (p.get("sales_rank") or "").strip()
        return f"🏆 <b>{_esc(rk)}</b>" if rk else None

    if name == "features":
        feats = p.get("features") or []
        picked = []
        for f in feats[:2]:
            f = (f or "").strip()
            if not f:
                continue
            if len(f) > 110:
                f = f[:107].rstrip() + "..."
            picked.append(f"• {_esc(f)}")
        return "\n".join(picked) if picked else None

    return None


# =============================================================================
# MAIN BUILDER
# =============================================================================
def build_amazon_caption(product: dict, short_link: str, cfg: dict,
                         has_image: bool = True):
    """
    Returns (caption_html, skipped_fields)
    skipped_fields — wo on fields jo jagah kam hone se chhoot gaye.
    """
    cfg      = cfg or {}
    fields   = cfg.get("amz_fields", {})
    detailed = cfg.get("amz_detailed", True)
    hdr      = cfg.get("header", {})
    ftr      = cfg.get("footer", {})
    btns     = cfg.get("buttons", {})

    limit = (PHOTO_CAPTION_LIMIT if has_image else TEXT_LIMIT) - 8

    head_line = (hdr.get("text") or "").strip() if hdr.get("enabled") else ""
    foot_line = (ftr.get("text") or "").strip() if ftr.get("enabled") else ""

    # Buy Now button ON hai to link usme hai — caption mein dobara nahi chahiye.
    buy_on    = bool(btns.get("buy", {}).get("enabled"))
    show_link = fields.get("link", True) and not buy_on

    link_line = ""
    if show_link and short_link:
        # Link bold, par <a href> ke BINA — Telegram khud link banata hai aur tap pe
        # seedha khulta hai. <a href> lagane pe Telegram har baar "Open Link?" poochta hai.
        shown = display_link(short_link) or short_link
        link_line = (f'🛒 <b>{_esc(shown)}</b>' if cfg.get("bold_links", True)
                     else f'🛒 {_esc(shown)}')

    # Header / footer / link ki jagah pehle se reserve
    reserved = 0
    if head_line:
        reserved += _u16(head_line) + 2
    if foot_line:
        reserved += _u16(foot_line) + 2
    if link_line:
        reserved += _visible_len(link_line) + 2

    # Price drop alert wali post — "₹999 → ₹799" line hamesha dikhe
    drop_note = (product.get("drop_note") or "").strip()
    drop_line = f"🔻 <b>{_esc(drop_note)}</b>" if drop_note else ""
    if drop_line:
        reserved += _visible_len(drop_line) + 1

    budget  = limit - reserved
    skipped = []
    body    = []

    if not detailed:
        # MINIMAL MODE — sirf price (aur link, jo alag se lagta hai)
        price_line = _render_field("price", product)
        if not price_line:
            mrp = (product.get("actual_price") or "").strip()
            if mrp:
                price_line = f"💰 Price: <b>{_esc(mrp)}</b>"
        if price_line and _visible_len(price_line) <= budget:
            body.append(price_line)
    else:
        for name in FIELD_ORDER:
            if not fields.get(name, False):
                continue
            line = _render_field(name, product)
            if not line:
                continue
            cost = _visible_len(line) + 1
            if cost > budget:
                skipped.append(name)
                continue
            body.append(line)
            budget -= cost

    if drop_line:
        at = 1 if (body and body[0].startswith("🔥")) else 0
        body.insert(at, drop_line)

    # Title ke baad ek khaali line — title aur MRP/price ke beech thodi jagah
    if len(body) > 1 and body[0].startswith("🔥"):
        body.insert(1, "")

    # ── Assemble ──────────────────────────────────────────────────────────
    parts = []
    if head_line:
        parts.append(_esc(head_line))
        parts.append("")
    if body:
        parts.append("\n".join(body))
        parts.append("")
    if link_line:
        parts.append(link_line)
    if foot_line:
        if link_line:
            parts.append("")
        parts.append(_esc(foot_line))

    caption = "\n".join(parts).strip()
    if not caption:
        caption = link_line or (_render_field("price", product) or "🔥 Deal")

    return caption, skipped


def wrap_plain_post(body_html: str, cfg: dict, has_image: bool = False) -> str:
    """Non-Amazon post — header/footer lagao aur limit ke andar rakho."""
    cfg = cfg or {}
    hdr = cfg.get("header", {})
    ftr = cfg.get("footer", {})

    head_line = (hdr.get("text") or "").strip() if hdr.get("enabled") else ""
    foot_line = (ftr.get("text") or "").strip() if ftr.get("enabled") else ""

    limit    = (PHOTO_CAPTION_LIMIT if has_image else TEXT_LIMIT) - 8
    reserved = 0
    if head_line:
        reserved += _u16(head_line) + 2
    if foot_line:
        reserved += _u16(foot_line) + 2

    body = _safe_truncate(body_html, max(40, limit - reserved))

    parts = []
    if head_line:
        parts.append(_esc(head_line))
        parts.append("")
    parts.append(body)
    if foot_line:
        parts.append("")
        parts.append(_esc(foot_line))
    return "\n".join(parts).strip()

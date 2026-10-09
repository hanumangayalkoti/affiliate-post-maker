"""
engine.py — posting ka poora engine. Har post ek TASK ke hisaab se jaati hai:
task ka Destination, uska affiliate tag, uski saari settings. User ke plan
(tier) ki limit — daily messages aur Image Card — yahin lagti hai.
"""
import io
import re
import html as html_lib
import asyncio
import logging
import aiohttp
from collections import deque
from datetime import datetime

from telegram import InlineKeyboardMarkup, InputFile
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest

from amazon_api import (
    is_amazon_url, is_amazon_search_url, resolve_amazon_url,
    extract_asin, get_products_by_asins,
    make_affiliate_url, make_cart_url, get_short_affiliate_link, display_link, fetch_error,
    is_known_amazon, find_amazon_behind_many,
)
from caption import build_amazon_caption, wrap_plain_post, FIELD_LABELS
from database import (
    claim_posted, release_posted, log_post, posts_today, normalise_caption,
)
from users import limits
from watermark import apply_watermark
from card import render_card
from ui import btn, tr, chan

logger = logging.getLogger(__name__)
esc = html_lib.escape

# Telegram channel limit ~20 msg/min. 4 second = 15/min — andar rehne ke liye.
POST_GAP_SECONDS = 4.0
MAX_PER_MESSAGE  = 15

SELF_MARKER = "\u2063"        # invisible — bot apne message pehchanne ke liye

# ── TIMEZONE ─────────────────────────────────────────────────────────────
from storage import LOCAL_TZ, to_local, local_day_start_utc, list_tasks, get_task  # noqa: E402
from store_ids import find_product_keys  # noqa: E402


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def fmt_date(dt) -> str:
    """DB ka UTC time → IST mein saaf format."""
    if not dt:
        return "—"
    return to_local(dt).strftime("%d %b %Y, %I:%M %p")


def fmt_short(dt) -> str:
    if not dt:
        return "—"
    return to_local(dt).strftime("%d %b %H:%M")


def day_start_naive() -> datetime:
    """Aaj (IST) ki shuruaat — DB queries ke liye UTC mein."""
    return local_day_start_utc()


URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)

_own_msg_ids = deque(maxlen=2000)
_own_msg_set = set()

def remember_own(m):
    if not m:
        return m
    try:
        key = (m.chat_id, m.message_id)
    except Exception:
        return m
    if len(_own_msg_ids) == _own_msg_ids.maxlen:
        _own_msg_set.discard(_own_msg_ids[0])
    _own_msg_ids.append(key)
    _own_msg_set.add(key)
    return m


def is_own_message(msg, bot_id) -> bool:
    if msg.from_user and msg.from_user.id == bot_id:
        return True
    try:
        if (msg.chat_id, msg.message_id) in _own_msg_set:
            return True
    except Exception:
        pass
    return SELF_MARKER in (msg.text or msg.caption or "")


# =============================================================================
# BASIC HELPERS
# =============================================================================
def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text) if text else []


def hidden_link_urls(entities) -> list:
    """Text ke peeche chhupe link (jaise 'Buy Now' pe link) — inme bhi Amazon ho sakta hai."""
    out = []
    for e in (entities or []):
        if str(getattr(e.type, "value", e.type)) == "text_link" and getattr(e, "url", None):
            out.append(e.url)
    return out


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


async def get_amazon_urls_deep(urls: list) -> list:
    """Seedhe Amazon links + wo short links (amzn-to.co, bit.ly...) jinke peeche
    Amazon product chhupa hai. Order message wala hi rehta hai."""
    hidden = set(await find_amazon_behind_many(urls))
    out = []
    for u in urls:
        if u not in out and (is_amazon_url(u) or u in hidden):
            out.append(u)
    return out


async def _download_image(url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Image download fail: {e}")
    return None


async def _get_photo_bytes(bot, file_id: str):
    if not file_id:
        return None
    try:
        file = await bot.get_file(file_id)
        return bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download fail: {e}")
    return None


def chat_matches(chat, ident: str) -> bool:
    if not chat or not ident:
        return False
    ident = str(ident).strip()
    if not ident:
        return False
    if ident.startswith("@"):
        return (chat.username or "").lower() == ident[1:].lower()
    try:
        return chat.id == int(ident)
    except (ValueError, TypeError):
        return (chat.username or "").lower() == ident.lower()


def same_channel(a: str, b: str) -> bool:
    x = (str(a or "")).strip().lstrip("@").lower()
    y = (str(b or "")).strip().lstrip("@").lower()
    return bool(x) and x == y


async def dm_user(bot, uid: int, text: str, **kwargs):
    try:
        return await bot.send_message(chat_id=uid, text=text, **kwargs)
    except Forbidden:
        from users import mark_bot_blocked
        mark_bot_blocked(uid)
    except Exception as e:
        logger.error(f"DM fail ({uid}): {e}")
    return None


async def _edit_or_notify(wait_msg, notify, text, **kwargs):
    footer = getattr(notify, "footer", "")
    if footer and footer not in text:
        text += footer                       # task / channel / tag har reply mein
        kwargs.setdefault("parse_mode", ParseMode.HTML)
    if wait_msg:
        try:
            await wait_msg.edit_text(text + SELF_MARKER, **kwargs)
            return
        except Exception:
            pass
    await notify(text, **kwargs)


async def _delete_quiet(m):
    if m:
        try:
            await m.delete()
        except Exception:
            pass


# =============================================================================
# ENTITY / HTML
# =============================================================================
def _py_to_utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


class _Ent:
    __slots__ = ("offset", "length", "type", "url")

    def __init__(self, offset, length, type_, url=None):
        self.offset = offset
        self.length = length
        self.type   = type_
        self.url    = url


def _clone_ent(ent, offset=None, length=None) -> _Ent:
    return _Ent(ent.offset if offset is None else offset,
                ent.length if length is None else length,
                ent.type, getattr(ent, "url", None))


def ents_to_json(entities) -> list:
    return [{"offset": e.offset, "length": e.length,
             "type": str(getattr(e.type, "value", e.type)),
             "url": getattr(e, "url", None)} for e in (entities or [])]


def ents_from_json(raw) -> list:
    return [_Ent(d["offset"], d["length"], d["type"], d.get("url")) for d in (raw or [])]


_URL_CHAR_RE = re.compile(r"[^\s\]\[<>\"']")


def _find_whole_url(text: str, url: str, start: int = 0) -> int:
    """`url` ki position jahan wo POORA link hai (kisi lambe link ka hissa nahi)."""
    idx = text.find(url, start)
    while idx >= 0:
        end = idx + len(url)
        if end >= len(text) or not _URL_CHAR_RE.match(text[end]):
            return idx
        idx = text.find(url, idx + 1)
    return -1


def replace_url_keep_entities(text: str, entities: list, old_url: str, new_url: str,
                              start: int = 0, return_pos: bool = False):
    """URL badlo aur entity offsets bhi shift karo — warna formatting khisak jaati hai."""
    idx = _find_whole_url(text, old_url, start) if old_url and old_url != new_url else -1
    if idx < 0:
        return (text, entities, -1) if return_pos else (text, entities)

    start_u16 = _py_to_utf16_len(text[:idx])
    old_u16   = _py_to_utf16_len(old_url)
    new_u16   = _py_to_utf16_len(new_url)
    end_u16   = start_u16 + old_u16
    delta     = new_u16 - old_u16

    new_text = text[:idx] + new_url + text[idx + len(old_url):]

    new_ents = []
    for ent in (entities or []):
        s = ent.offset
        e = ent.offset + ent.length
        if e <= start_u16:
            new_ents.append(_clone_ent(ent))
        elif s >= end_u16:
            new_ents.append(_clone_ent(ent, offset=s + delta))
        elif s <= start_u16 and e >= end_u16:
            new_ents.append(_clone_ent(ent, length=ent.length + delta))
    if return_pos:
        return new_text, new_ents, idx + len(new_url)
    return new_text, new_ents


async def replace_amazon_links(text: str, entities: list, urls: list, tag: str):
    """Har Amazon link (har jagah jahan aaya) pe user ka tag — dikhne wale + chhupe hue."""
    uniq = []
    for u in urls:
        if u not in uniq and is_known_amazon(u):
            uniq.append(u)
    for url in sorted(uniq, key=len, reverse=True):
        if url not in text:
            continue
        try:
            short = await get_short_affiliate_link(url, tag)
        except Exception as e:
            logger.error(f"Affiliate link fail: {e}")
            continue
        # Asli link, tag ke saath, KHULA dikhe (https://www.amazon.in/dp/ASIN?tag=..)
        # — chhupa link nahi, taaki Telegram "Open this link?" na pooche.
        shown = display_link(short) or short
        pos = 0
        for _ in range(20):
            text, entities, pos = replace_url_keep_entities(text, entities, url, shown,
                                                            start=pos, return_pos=True)
            if pos < 0:
                break
    # Text-link (hidden link) entities mein bhi Amazon link ho sakta hai
    fixed = []
    for ent in (entities or []):
        if str(getattr(ent.type, "value", ent.type)) == "text_link" and is_known_amazon(ent.url or ""):
            try:
                new_url = await get_short_affiliate_link(ent.url, tag)
                ent = _Ent(ent.offset, ent.length, "text_link", new_url)
            except Exception:
                pass
        fixed.append(ent)
    return text, fixed


# =============================================================================
# DOOSRE CHANNEL KA PROMO HATAO — @username aur Telegram links
# =============================================================================
_TG_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/\S*", re.IGNORECASE)
_MENTION_RE = re.compile(r"(?<![\w@/.])@[A-Za-z][A-Za-z0-9_]{3,31}\b")
# @username ke saath ye shabd hon to poori line promo hai ("Join @xyz for more")
_PROMO_WORDS_RE = re.compile(
    r"\b(join|follow|subscribe|channel|group|credit|credits|via|source|deal\s*by|posted\s*by|"
    r"share|more\s*deals|for\s*more)\b", re.IGNORECASE)


def _delete_spans(text: str, entities: list, spans: list):
    """text ke kuch hisse (python index) hatao aur entity offsets theek karo."""
    spans = sorted((max(0, s), min(len(text), e)) for s, e in spans if e > s)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    if not merged:
        return text, list(entities or [])
    spans16 = [(_py_to_utf16_len(text[:s]), _py_to_utf16_len(text[:e])) for s, e in merged]

    def shift(x):
        cut = 0
        for s, e in spans16:
            if x >= e:
                cut += e - s
            elif x > s:
                cut += x - s
        return x - cut

    new_ents = []
    for ent in (entities or []):
        a, b = shift(ent.offset), shift(ent.offset + ent.length)
        if b > a:
            new_ents.append(_clone_ent(ent, offset=a, length=b - a))
    parts, last = [], 0
    for s, e in merged:
        parts.append(text[last:s])
        last = e
    parts.append(text[last:])
    return "".join(parts), new_ents


def strip_promo(text: str, entities: list, keep: tuple = ()):
    """
    Doosre channel ka promo hatao: Telegram link wali poori line (dikhne wala
    t.me link ho ya kisi text ke peeche chhupa), aur har @username.
    Baaki caption ("Loot Free", "Apply Coupon"...) jaisa tha waisa rehta hai.
    keep — apne channel ke usernames (bina @), jo kabhi nahi hatte.
    """
    keep = {k.lstrip("@").lower() for k in keep if k}

    def foreign(m) -> bool:
        return m.group(0)[1:].lower() not in keep

    def own_tg(line_or_url: str) -> bool:
        m = _TG_LINK_RE.search(line_or_url or "")
        if not m:
            return False
        path = re.sub(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/", "", m.group(0), flags=re.I)
        return path.split("/")[0].split("?")[0].lower() in keep
    if not text:
        return text, list(entities or [])
    entities = list(entities or [])
    tg_ranges = [(e.offset, e.offset + e.length) for e in entities
                 if str(getattr(e.type, "value", e.type)) == "text_link" and _TG_LINK_RE.search(e.url or "")
                 and not own_tg(e.url)]
    spans, pos = [], 0
    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        s16 = _py_to_utf16_len(text[:start])
        e16 = s16 + _py_to_utf16_len(line)
        promo_mention = any(foreign(m) for m in _MENTION_RE.finditer(line)) and _PROMO_WORDS_RE.search(line)
        tg_line = _TG_LINK_RE.search(line) and not own_tg(line)
        if (tg_line or promo_mention
                or any(a < e16 and b > s16 for a, b in tg_ranges)):
            spans.append((start, end + 1))          # newline samet poori line
        pos = end + 1
    for m in _MENTION_RE.finditer(text):
        if not foreign(m):
            continue
        s = m.start() - 1 if m.start() > 0 and text[m.start() - 1] == " " else m.start()
        spans.append((s, m.end()))
    text, entities = _delete_spans(text, entities, spans)
    # 2 se zyada khaali lines → 1 khaali line; shuru/aakhir ki khaali jagah hatao
    spans = [(m.start() + 2, m.end()) for m in re.finditer(r"\n{3,}", text)]
    text, entities = _delete_spans(text, entities, spans)
    lead = len(text) - len(text.lstrip())
    trail = len(text.rstrip())
    return _delete_spans(text, entities, [(0, lead), (trail, len(text))])


def remove_footer(plain_text: str, entities: list):
    lines = plain_text.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    changed = True
    while changed and lines:
        changed = False
        if FOOTER_LINE_PATTERN.match(lines[-1].strip()):
            lines.pop()
            changed = True
    cleaned = '\n'.join(lines).rstrip()
    if not cleaned.strip() and plain_text.strip():
        return plain_text.rstrip(), list(entities or [])
    cutoff = _py_to_utf16_len(cleaned)
    return cleaned, [e for e in (entities or []) if e.offset + e.length <= cutoff]


def _build_utf16_map(text: str) -> list:
    mapping = []
    for py_idx, ch in enumerate(text):
        mapping.append(py_idx)
        if ord(ch) > 0xFFFF:
            mapping.append(py_idx)
    mapping.append(len(text))
    return mapping


_LOOKS_LIKE_URL = re.compile(r"^\s*(?:https?://|www\.)\S+\s*$", re.I)


def entities_to_html(text: str, entities: list, bold_links: bool = True) -> str:
    """Telegram entities → HTML. bold_links=False: links (url / text_link) bold NAHI
    honge — task ka 'Bold Link' toggle OFF."""
    if not entities:
        return html_lib.escape(text)

    utf16_map  = _build_utf16_map(text)
    open_tags  = [""] * len(text)
    close_tags = [""] * len(text)

    pairs = {
        "url":           ("<b>", "</b>"),
        "bold":          ("<b>", "</b>"),
        "italic":        ("<i>", "</i>"),
        "underline":     ("<u>", "</u>"),
        "strikethrough": ("<s>", "</s>"),
        "code":          ("<code>", "</code>"),
        "pre":           ("<pre>", "</pre>"),
        "spoiler":       ("<tg-spoiler>", "</tg-spoiler>"),
    }
    for ent in sorted(entities, key=lambda e: (e.offset, -e.length)):
        s_u16 = ent.offset
        e_u16 = ent.offset + ent.length
        s = utf16_map[s_u16] if s_u16 < len(utf16_map) else s_u16
        e = utf16_map[e_u16] if e_u16 < len(utf16_map) else e_u16
        if e > len(text) or s >= len(text) or e <= s:
            continue
        etype = str(getattr(ent.type, "value", ent.type))
        b_open, b_close = ("<b>", "</b>") if bold_links else ("", "")
        if etype == "url" and not bold_links:
            continue                         # sada link, Telegram khud link banata hai
        if etype == "text_link" and _LOOKS_LIKE_URL.match(text[s:e]):
            # Dikhne wala text khud ek link hai — <a href> nahi; Telegram khud link
            # banayega aur tap pe seedha kholega ("Open Link?" nahi aayega)
            open_tags[s]    = b_open + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + b_close
            continue
        if etype == "text_link":
            url = html_lib.escape(ent.url or "")
            open_tags[s]    = f'<a href="{url}">{b_open}' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + f'{b_close}</a>'
        elif etype in pairs:
            o, c = pairs[etype]
            open_tags[s]    = o + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + c

    result = []
    for i, ch in enumerate(text):
        result.append(open_tags[i])
        result.append(html_lib.escape(ch))
        result.append(close_tags[i])
    return ''.join(result)


# =============================================================================
# BUTTONS UNDER POST
# =============================================================================
def build_final_markup(config: dict, asin: str = ""):
    """Buy Now / Add to Cart sirf Amazon post pe (user ke tag ke saath)."""
    btns = config.get("buttons", {})
    tag  = config.get("tag", "")
    rows = []

    amz_row = []
    if asin:
        buy = btns.get("buy", {})
        if buy.get("enabled"):
            amz_row.append(btn(buy.get("label") or "⚡ Buy Now", buy.get("style", ""),
                               url=make_affiliate_url(asin, tag)))
        cart = btns.get("cart", {})
        if cart.get("enabled"):
            amz_row.append(btn(cart.get("label") or "🛒 Add to Cart", cart.get("style", ""),
                               url=make_cart_url(asin, tag)))
    if amz_row:
        rows.append(amz_row)

    row = []
    for key in ("btn1", "btn2"):
        b = btns.get(key, {})
        if b.get("enabled") and b.get("label") and b.get("url"):
            row.append(btn(b["label"], b.get("style", ""), url=b["url"]))
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows) if rows else None


async def _send_with_retry(coro_factory, tries: int = 3):
    last_err = None
    for attempt in range(tries):
        try:
            return await coro_factory()
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 5)) + 1
            logger.warning(f"429 — {wait:.0f}s ruk raha hoon (try {attempt+1})")
            await asyncio.sleep(wait)
            last_err = e
        except TimedOut:
            # Timeout ka matlab post shayad pahunch chuki hai — dobara bheji to double post
            raise
        except NetworkError as e:
            if isinstance(e, BadRequest):
                raise
            await asyncio.sleep(2 + attempt * 2)
            last_err = e
    if last_err:
        raise last_err
    return None


def _friendly_error(e: Exception, lang: str = "hi") -> str:
    """Telegram ki error ko aam bhasha mein."""
    s = str(e)
    low = s.lower()
    if "chat not found" in low:
        return tr(lang, "Destination channel not found — set it again in /tasks.",
                  "Destination channel nahi mila — /tasks mein dobara set karein.")
    if "not enough rights" in low or "need administrator rights" in low or isinstance(e, Forbidden):
        return tr(lang, "The bot is not an admin in the channel, or 'Post Messages' is OFF.",
                  "Bot channel mein admin nahi hai, ya 'Post Messages' permission band hai.")
    return s[:150]


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plain(s: str) -> str:
    return html_lib.unescape(_HTML_TAG_RE.sub("", s or ""))


async def deliver(send_fn, kwargs: dict, photo_bytes: bytes = None, photo_name: str = "post.jpg"):
    """
    Post bhejo. Do galtiyon pe post fail nahi hone dete:
      • formatting (HTML) toot gayi  → bina formatting dobara bhejo
      • button ka link galat          → bina buttons dobara bhejo
    """
    kw = dict(kwargs)
    tried = set()

    def build():
        d = dict(kw)
        if photo_bytes is not None:
            d["photo"] = InputFile(io.BytesIO(photo_bytes), filename=photo_name)
        return d

    while True:
        try:
            return await _send_with_retry(lambda: send_fn(**build()))
        except BadRequest as e:
            low = str(e).lower()
            if "parse" in low and "entit" in low and "parse" not in tried:
                tried.add("parse")
                logger.warning(f"HTML parse fail — plain text mein bhej raha hoon: {e}")
                for k in ("text", "caption"):
                    if kw.get(k):
                        kw[k] = html_to_plain(kw[k])
                kw["parse_mode"] = None
                continue
            if (kw.get("reply_markup") is not None and "markup" not in tried
                    and ("button" in low or "url" in low)):
                tried.add("markup")
                logger.warning(f"Button fail — bina button bhej raha hoon: {e}")
                kw["reply_markup"] = None
                continue
            raise


async def make_post_image(raw: bytes, product: dict, cfg: dict, allow_card: bool = True):
    """
    Amazon photo → post wali photo. Card ON (aur plan mein allowed) hai to card
    (watermark + Amazon badge andar hi), warna seedhi photo + watermark + badge.
    Returns (bytes, card_bana?). Pillow ka kaam alag thread mein — bot baaki
    users ke liye ruke nahi.
    """
    card_cfg = cfg.get("card") or {}
    wm = cfg.get("watermark", {})
    badge = bool(cfg.get("amazon_badge", True))
    if not _wm_on(cfg, "amazon"):
        wm = {}                              # Amazon posts pe watermark OFF
    if allow_card and card_cfg.get("enabled"):
        out = await asyncio.to_thread(render_card, raw, product, card_cfg, wm, badge)
        if out:
            return out, True
    if _wm_on(cfg, "amazon") or badge:
        out = await asyncio.to_thread(apply_watermark, raw, wm if _wm_on(cfg, "amazon") else {},
                                      card_cfg.get("font", "poppins"), badge)
        return out, False
    return raw, False


def _wm_on(cfg: dict, kind: str = "amazon") -> bool:
    """Watermark lagana hai? kind = 'amazon' / 'other' — task mein dono ka alag ON/OFF."""
    wm = cfg.get("watermark", {})
    if not (wm.get("enabled") and (wm.get("text") or "").strip()):
        return False
    return bool(cfg.get("wm_amazon" if kind == "amazon" else "wm_other", True))


# =============================================================================
# POSTING
# =============================================================================
async def post_amazon_product(context, uid: int, task: dict, product: dict, lang: str = "hi"):
    """
    Ek Amazon product task ke Destination pe post karo.
    Returns (status, detail, note) — posted / duplicate / error
    """
    cfg      = task["cfg"]
    tid      = task["id"]
    asin     = product.get("asin", "")
    channel  = str(cfg.get("channel", "")).strip()
    tag      = cfg.get("tag", "")
    silent   = cfg.get("silent", True)
    detailed = cfg.get("amz_detailed", True)
    fields   = cfg.get("amz_fields", {})
    title    = (product.get("title") or "").strip()
    title_key = normalise_caption(title)

    dup_keys = (("a:" + asin if asin else ""), ("t:" + title_key if title_key else "")) \
        if cfg.get("dup_check", True) else ()
    ok, when = claim_posted(uid, tid, *dup_keys)
    if not ok:
        return "duplicate", f"{title[:55] or asin} — {when}", ""

    allow_card = limits(uid)["card"]
    card_on    = allow_card and bool((cfg.get("card") or {}).get("enabled"))
    want_image = card_on or (detailed and fields.get("image", True))
    short_link = make_affiliate_url(asin, tag)

    img_bytes, used_card = None, False
    if want_image and product.get("image_url"):
        raw = await _download_image(product["image_url"])
        if raw:
            img_bytes, used_card = await make_post_image(raw, product, cfg, allow_card)

    caption, _ = build_amazon_caption(product, short_link, cfg, has_image=bool(img_bytes))
    markup = build_final_markup(cfg, asin=asin)

    note = ""
    if img_bytes:
        note = ("Image Card" if used_card else "Amazon") + (" + Watermark" if _wm_on(cfg, "amazon") else "")

    try:
        if img_bytes:
            await deliver(context.bot.send_photo,
                          dict(chat_id=channel, caption=caption, parse_mode=ParseMode.HTML,
                               reply_markup=markup, disable_notification=silent),
                          photo_bytes=img_bytes, photo_name=f"{asin}.jpg")
        else:
            await deliver(context.bot.send_message,
                          dict(chat_id=channel, text=caption, parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True, reply_markup=markup,
                               disable_notification=silent))
        log_post(uid, tid, "amazon", asin, title)
        return "posted", title or asin, note
    except Exception as e:
        release_posted(uid, tid, *dup_keys)
        logger.error(f"Post fail {uid}/{tid}/{asin}: {e}")
        return "error", _friendly_error(e, lang), ""


def _strip_if_on(text: str, entities: list, cfg: dict):
    """Task mein "🚫 Remove t.me link and Username" ON ho (default) tabhi promo hatao."""
    if not cfg.get("strip_promo", True):
        return text, list(entities or [])
    return strip_promo(text, entities, _own_handles(cfg))


def _own_handles(cfg: dict) -> tuple:
    """Task ke apne channels — inke @username / t.me link post mein reh sakte hain."""
    return tuple(h for h in (cfg.get("channel_username"), cfg.get("source_username")) if h)


_NODATA_REASONS = {
    "busy":           ("Amazon API was busy (too many requests) — tried twice.",
                       "Amazon API busy tha (bahut requests) — 2 baar try kiya."),
    "server":         ("Amazon server error — tried twice.",
                       "Amazon server mein error — 2 baar try kiya."),
    "network":        ("Could not reach Amazon (network/timeout) — tried twice.",
                       "Amazon tak connection nahi hua (network/timeout) — 2 baar try kiya."),
    "auth":           ("Amazon API login (token) failed — admin should check the API keys.",
                       "Amazon API login (token) fail — admin API keys check kare."),
    "not_accessible": ("Amazon does not share this product's data through the API.",
                       "Amazon is product ka data API se nahi deta."),
    "invalid":        ("Amazon says this product is invalid / not available.",
                       "Amazon ke hisaab se ye product galat ya band hai."),
    "empty":          ("Amazon returned no data for this product.",
                       "Amazon ne is product ka koi data nahi bheja."),
}


def _nodata_reason(lang: str, asin: str) -> str:
    """Draft reply ke liye: details kyun nahi mili (ek line, '' agar pata nahi)."""
    r = _NODATA_REASONS.get(fetch_error(asin))
    return ("\n🔎 " + tr(lang, r[0], r[1])) if r else ""


async def post_amazon_original(context, uid: int, task: dict, msg, raw_plain: str, raw_entities: list,
                               amazon_urls: list, products: list, live: list, lang: str = "hi"):
    """
    MINIMAL mode — original post ka caption hi jaata hai ("Loot Free", "Apply
    Coupon"...). Amazon links pe user ka tag, doosre channel ke @username aur
    Telegram links hate hue. Photo: Image Card (ON ho to) → Amazon photo →
    original post ki photo. Returns (status, detail, note).
    """
    cfg     = task["cfg"]
    tid     = task["id"]
    tag     = cfg.get("tag", "")
    channel = str(cfg.get("channel", "")).strip()
    silent  = cfg.get("silent", True)
    asins   = [p["asin"] for p in products]

    dup_keys = tuple("a:" + a for a in asins) if cfg.get("dup_check", True) else ()
    ok, when = claim_posted(uid, tid, *dup_keys)
    if not ok:
        return "duplicate", when, ""

    cp, ce = remove_footer(raw_plain, raw_entities)
    cp, ce = await replace_amazon_links(cp, ce, amazon_urls, tag)
    cp, ce = _strip_if_on(cp, ce, cfg)
    body = entities_to_html(cp, ce, cfg.get("bold_links", True)) if cp.strip() else ""

    best = live[0] if live else None
    img_bytes, used_card, src = None, False, ""
    if best and best.get("image_url"):
        raw = await _download_image(best["image_url"])
        if raw:
            img_bytes, used_card = await make_post_image(raw, best, cfg, limits(uid)["card"])
            src = "Image Card" if used_card else "Amazon photo"
    if img_bytes is None and msg is not None and msg.photo:
        raw = await _get_photo_bytes(context.bot, msg.photo[-1].file_id)
        if raw and _wm_on(cfg, "amazon"):
            raw = await asyncio.to_thread(apply_watermark, raw, cfg.get("watermark", {}),
                                          (cfg.get("card") or {}).get("font", "poppins"))
        img_bytes, src = raw, tr(lang, "original photo", "original photo")
    if img_bytes and _wm_on(cfg, "amazon") and not used_card and src != "original photo":
        src += " + Watermark"

    markup = build_final_markup(cfg, asin=asins[0] if asins else "")
    try:
        if img_bytes:
            caption = wrap_plain_post(body, cfg, has_image=True) if body else None
            await deliver(context.bot.send_photo,
                          dict(chat_id=channel, caption=caption,
                               parse_mode=ParseMode.HTML if caption else None,
                               reply_markup=markup, disable_notification=silent),
                          photo_bytes=img_bytes, photo_name=f"{asins[0] if asins else 'post'}.jpg")
        else:
            if not body.strip():
                release_posted(uid, tid, *dup_keys)
                return "error", tr(lang, "empty post", "khali post"), ""
            await deliver(context.bot.send_message,
                          dict(chat_id=channel, text=wrap_plain_post(body, cfg, has_image=False),
                               parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                               reply_markup=markup, disable_notification=silent))
        title = ((best or {}).get("title") or cp or "")[:80]
        log_post(uid, tid, "amazon", asins[0] if asins else "", title)
        return "posted", title, src
    except Exception as e:
        release_posted(uid, tid, *dup_keys)
        logger.error(f"Original post fail {uid}/{tid}: {e}")
        return "error", _friendly_error(e, lang), ""


def _other_dup_key(payload: dict) -> str:
    """Purana tareeka — caption (link ke saath) se; text na ho to photo/video ki ID se."""
    k = normalise_caption(payload.get("text") or "")
    if k:
        return "c:" + k
    uid_ = payload.get("unique_id") or ""
    return ("f:" + uid_) if uid_ else ""


MIN_CAPTION_WORDS = 4      # isse chhota caption ("Loot deal 🔥") akela duplicate nahi maana jaata


def _caption_without_links(text: str) -> str:
    return URL_REGEX.sub(" ", text or "")


async def _other_dup_keys(payload: dict):
    """
    Non-Amazon duplicate keys — priority se:
      1) link kholke product number (Myntra / Flipkart / Shopsy) — EarnKaro har baar
         naya link deta hai, par product number wahi rehta hai
      2) link na khule → caption BINA link ke (sirf lamba caption, 4+ shabd)
      3) warna purana tareeka (caption + link / photo ID)
    Returns (keys_to_check, extra_keys_to_remember, reason)
    """
    text = payload.get("text") or ""
    urls = extract_urls(text) + hidden_link_urls(ents_from_json(payload.get("entities")))
    old = _other_dup_key(payload)
    if any(is_amazon_url(u) for u in urls):
        return [old], [], ""                       # Amazon search/deal page — pehle jaisa
    cap = normalise_caption(_caption_without_links(text))
    cap_key = ("n:" + cap) if len(cap.split()) >= MIN_CAPTION_WORDS else ""
    products = await find_product_keys(urls) if urls else []
    if products:
        # Product mila → sirf product se faisla. Caption bhi yaad rakho, taaki
        # baad mein link na khule tab bhi same caption pakda jaaye.
        return ["p:" + k for k in products], [cap_key], "product"
    if cap_key:
        return [cap_key], [], "caption"
    return [old], [], ""


async def post_other(context, uid: int, task: dict, payload: dict, lang: str = "hi",
                     wm_kind: str = "other"):
    """Non-Amazon post. Returns (status, detail)."""
    cfg     = task["cfg"]
    tid     = task["id"]
    channel = str(cfg.get("channel", "")).strip()
    silent  = cfg.get("silent", True)
    wm      = cfg.get("watermark", {})
    font    = (cfg.get("card") or {}).get("font", "poppins")

    text       = payload.get("text") or ""
    entities   = ents_from_json(payload.get("entities"))
    file_id    = payload.get("photo_file_id") or ""
    media_fid  = payload.get("media_file_id") or ""
    media_kind = payload.get("media_kind") or ""

    dup_keys, extra_keys, why = (await _other_dup_keys(payload)) if cfg.get("dup_check", True) \
        else ([], [], "")
    if why == "product" and len(dup_keys) > 1:
        # Kai product ek post mein: skip SIRF tab jab saare pehle post ho chuke hon.
        # Ek bhi naya ho to post jaati hai (nayi deal na chhoote). Jo naye the wahi
        # claim hote hain — fail hone pe wahi wapas hote hain.
        fresh_keys, when = [], None
        for k in dup_keys:
            got, w = claim_posted(uid, tid, k)
            if got:
                fresh_keys.append(k)
            else:
                when = when or w
        ok = bool(fresh_keys)
        dup_keys = fresh_keys
    else:
        ok, when = claim_posted(uid, tid, *dup_keys)
    if not ok:
        reason = {"product": tr(lang, "same product", "same product"),
                  "caption": tr(lang, "same caption", "same caption")}.get(why, "")
        reason = f" — {reason}" if reason else ""
        return "duplicate", tr(lang, f"non-Amazon post{reason} — {when} ago",
                               f"non-Amazon post{reason} — {when} pehle")
    for k in extra_keys:
        claim_posted(uid, tid, k)                 # sirf yaad rakhna — faisla upar ho chuka

    body_html = entities_to_html(text, entities, cfg.get("bold_links", True)) if text else ""

    try:
        if file_id:
            img_bytes = await _get_photo_bytes(context.bot, file_id)
            if img_bytes and _wm_on(cfg, wm_kind):
                img_bytes = await asyncio.to_thread(apply_watermark, img_bytes, wm, font)
            caption = wrap_plain_post(body_html, cfg, has_image=True) if text else None
            base = dict(chat_id=channel, caption=caption,
                        parse_mode=ParseMode.HTML if caption else None,
                        reply_markup=build_final_markup(cfg), disable_notification=silent)
            if img_bytes:
                await deliver(context.bot.send_photo, base, photo_bytes=img_bytes)
            else:
                await deliver(context.bot.send_photo, dict(base, photo=file_id))

        elif media_fid:
            caption = wrap_plain_post(body_html, cfg, has_image=True) if text else None
            sender = {
                "document":   context.bot.send_document,
                "video":      context.bot.send_video,
                "animation":  context.bot.send_animation,
                "video_note": context.bot.send_video_note,
            }.get(media_kind, context.bot.send_document)
            kwargs = {"chat_id": channel, "reply_markup": build_final_markup(cfg),
                      "disable_notification": silent}
            if media_kind != "video_note":
                kwargs["caption"] = caption
                kwargs["parse_mode"] = ParseMode.HTML if caption else None
            key = media_kind if media_kind in ("document", "video", "animation",
                                               "video_note") else "document"
            kwargs[key] = media_fid
            await deliver(sender, kwargs)

        else:
            if not body_html.strip():
                release_posted(uid, tid, *dup_keys, *extra_keys)
                return "error", tr(lang, "empty post", "khali post")
            await deliver(context.bot.send_message,
                          dict(chat_id=channel, text=wrap_plain_post(body_html, cfg, has_image=False),
                               parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                               reply_markup=build_final_markup(cfg), disable_notification=silent))

        log_post(uid, tid, "other", "", (text or "")[:80])
        return "posted", "non-Amazon post"
    except Exception as e:
        release_posted(uid, tid, *dup_keys, *extra_keys)
        logger.error(f"post_other fail ({uid}/{tid}): {e}")
        return "error", _friendly_error(e, lang)


# =============================================================================
# AMAZON LINK CLASSIFICATION
# =============================================================================
async def classify_amazon_urls(urls: list) -> dict:
    """ASIN check PEHLE — search se nikla product link product hi hai."""
    products, searches, unknown = [], [], []
    seen = set()
    for url in urls:
        try:
            resolved = await resolve_amazon_url(url)
        except Exception as e:
            logger.error(f"Resolve fail ({url[:50]}): {e}")
            resolved = url
        asin = extract_asin(resolved) or extract_asin(url)
        if asin:
            if asin in seen:
                continue
            seen.add(asin)
            products.append({"url": url, "resolved": resolved, "asin": asin})
        elif is_amazon_search_url(resolved) or is_amazon_search_url(url):
            searches.append(url)
        else:
            unknown.append(url)
    return {"products": products, "searches": searches, "unknown": unknown}


# =============================================================================
# CORE PROCESSOR
# =============================================================================
def _msg_payload(msg, text: str, entities) -> dict:
    p = {"text": text, "entities": ents_to_json(entities)}
    media = None
    if msg.photo:
        p["photo_file_id"] = msg.photo[-1].file_id
        media = msg.photo[-1]
    elif msg.document:
        p["media_file_id"], p["media_kind"] = msg.document.file_id, "document"
        media = msg.document
    elif msg.video:
        p["media_file_id"], p["media_kind"] = msg.video.file_id, "video"
        media = msg.video
    elif msg.animation:
        p["media_file_id"], p["media_kind"] = msg.animation.file_id, "animation"
        media = msg.animation
    elif msg.video_note:
        p["media_file_id"], p["media_kind"] = msg.video_note.file_id, "video_note"
        media = msg.video_note
    if media is not None:
        p["unique_id"] = getattr(media, "file_unique_id", "") or ""
    return p


def setup_problems(cfg: dict, lang: str = "hi") -> list:
    """Post karne se pehle task mein kya-kya set hona baaki hai."""
    probs = []
    if not (cfg.get("tag") or "").strip():
        probs.append(tr(lang, "🏷️ Affiliate tag is not set", "🏷️ Affiliate tag set nahi hai"))
    if not str(cfg.get("channel") or "").strip():
        probs.append(tr(lang, "📢 Destination channel is not set", "📢 Destination channel set nahi hai"))
    return probs


def posts_left_today(uid: int, task_id: int = None):
    """Is task mein aaj kitni post aur ho sakti hain (limit har task ki alag).
    task_id na ho to user ka sabse zyada bacha hua task. None = koi limit nahi (admin)."""
    lim = limits(uid)
    if lim.get("key") == "admin":
        return None
    if not lim.get("key"):
        return 0
    if task_id is not None:
        return max(0, lim["daily"] - posts_today(uid, task_id))
    tids = [t["id"] for t in list_tasks(uid)]
    if not tids:
        return lim["daily"]
    return max(max(0, lim["daily"] - posts_today(uid, t)) for t in tids)


async def process_and_post(context, uid: int, msg, notify, task: dict, lang: str = "hi",
                           source_tag: str = ""):
    """Ek message (DM ya Draft channel se) ko task ke hisaab se post karo."""
    cfg  = task["cfg"]
    tname = task_name(task, lang)

    if msg.caption is not None:
        raw_plain, raw_entities = msg.caption or "", list(msg.caption_entities or [])
        has_photo = True
    elif msg.text:
        raw_plain, raw_entities = msg.text or "", list(msg.entities or [])
        has_photo = False
    else:
        raw_plain, raw_entities = "", []
        has_photo = bool(msg.photo)

    all_urls    = extract_urls(raw_plain) + hidden_link_urls(raw_entities)
    # Third-party short links (amzn-to.co jaise) bhi kholke dekhte hain
    amazon_urls = await get_amazon_urls_deep(all_urls)

    channel = str(cfg.get("channel", "")).strip()
    tag     = cfg.get("tag", "")
    shown   = chan(cfg.get("channel_title"), cfg.get("channel_username"), esc(channel) or "—")
    # HAR reply (post hua / skip / duplicate / limit / fail) ke neeche: kaunsa task,
    # kis channel pe. Amazon link ho to kaunsa tag laga; Non-Amazon ho to saaf likho
    # ki Non-Amazon link hai (uspe tag nahi lagta). Ek Draft kai accounts ka ho sakta
    # hai — isse saaf dikhta hai ki reply kiske task ka hai.
    if amazon_urls:
        kind_line = f"\n🏷️ Tag: <code>{esc(tag)}</code>" if tag else ""
    else:
        kind_line = tr(lang, "\n🔗 Non-Amazon link", "\n🔗 Non-Amazon link")
    footer = f"\n📋 {esc(tname)} → 📢 {shown}" + kind_line + source_tag
    footer_plain = footer

    _send_notify = notify

    async def notify(text, **kwargs):
        if footer not in text:
            text += footer
        kwargs.setdefault("parse_mode", ParseMode.HTML)
        return await _send_notify(text, **kwargs)
    notify.footer = footer           # _edit_or_notify bhi yahi footer lagata hai

    if not raw_plain.strip() and not all_urls and not has_photo and not (
            msg.document or msg.video or msg.animation or msg.video_note):
        await notify(tr(lang, "⚠️ No text or link found in this message.",
                        "⚠️ Is message mein koi text ya link nahi mila."))
        return

    probs = setup_problems(cfg, lang)
    if probs:
        await notify(tr(lang, f"⚠️ <b>{esc(tname)} — setup is incomplete:</b>\n\n",
                        f"⚠️ <b>{esc(tname)} — setup adhoora hai:</b>\n\n")
                     + "\n".join(probs)
                     + tr(lang, "\n\nOpen /tasks to finish it.", "\n\n/tasks se poora karein."),
                     parse_mode=ParseMode.HTML)
        return

    # Amazon / Non-Amazon filter
    if amazon_urls and not cfg.get("allow_amazon", True):
        await notify(tr(lang, f"⏭️ Skipped — <b>{esc(tname)}</b> posts only Non-Amazon deals.",
                        f"⏭️ Skip — <b>{esc(tname)}</b> mein sirf Non-Amazon posts jaati hain."),
                     parse_mode=ParseMode.HTML)
        return
    if not amazon_urls and not cfg.get("allow_other", True):
        await notify(tr(lang, f"⏭️ Skipped — <b>{esc(tname)}</b> posts only Amazon deals.",
                        f"⏭️ Skip — <b>{esc(tname)}</b> mein sirf Amazon posts jaati hain."),
                     parse_mode=ParseMode.HTML)
        return

    left = posts_left_today(uid, task["id"])
    if left is not None and left <= 0:
        lim = limits(uid)
        await notify(tr(lang,
                        f"🚫 <b>{esc(tname)} — today's limit reached</b> ({lim.get('daily', 0)} posts/day "
                        "per task).\nIt resets at 12:00 midnight. Need more? /plan",
                        f"🚫 <b>{esc(tname)} — aaj ki limit poori</b> ({lim.get('daily', 0)} post/din "
                        "har task).\nRaat 12 baje reset hogi. Zyada chahiye? /plan"),
                     parse_mode=ParseMode.HTML)
        return

    # ==========================================================================
    # AMAZON
    # ==========================================================================
    if amazon_urls:
        # Draft channel mein "Checking..." wala extra message nahi — Telegram ek
        # channel mein ~20 message/minute hi bhejne deta hai; sale mein har deal pe
        # 2 reply (× har task) se line lag jaati thi aur bot slow ho jaata tha.
        wait_msg = None if source_tag else await notify(
            tr(lang, "⏳ Checking Amazon links...", "⏳ Amazon links check ho rahe hain..."))
        buckets  = await classify_amazon_urls(amazon_urls)
        products = buckets["products"]
        searches = buckets["searches"]
        unknown  = buckets["unknown"]

        if cfg.get("search_links") and searches:
            unknown, searches = unknown + searches, []

        if not products and not unknown:
            await _edit_or_notify(
                wait_msg, notify,
                tr(lang,
                   f"🚫 <b>Skipped!</b> Only Amazon search/deals pages found ({len(searches)}).\n"
                   "<i>To post these too, turn ON 'Search Links' in the task settings"
                   + (" and turn the Discount Filter OFF" if _min_discount(cfg) else "") + ".</i>",
                   f"🚫 <b>Skip!</b> Sirf Amazon search/deals page mile ({len(searches)}).\n"
                   "<i>Ye bhi post karne hain to task settings mein 'Search Links' ON karein"
                   + (" aur Discount Filter OFF karein" if _min_discount(cfg) else "") + ".</i>"),
                parse_mode=ParseMode.HTML)
            return

        # Post aane ke baad Amazon/Telegram ke intezaar mein user ne settings badli
        # ho sakti hain (Discount Filter, buttons...) — taaza settings se chalo.
        fresh = await _still_running(task, uid, lang, tname, wait_msg, notify)
        if fresh is None:
            return
        task = fresh
        cfg = task["cfg"]
        md = _min_discount(cfg)

        if products and not cfg.get("amz_detailed", True):
            # MINIMAL — original caption, ek hi post (kitne bhi product links hon)
            fetched = await get_products_by_asins([p["asin"] for p in products])
            live = [fetched[p["asin"]] for p in products if p["asin"] in fetched]
            if not live and md:
                # Filter ON aur kisi product ki detail nahi mili → discount pata nahi, skip
                await _edit_or_notify(wait_msg, notify,
                                      _nodata_skip_text(lang, products[0]["asin"], len(products), md),
                                      parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                return
            # Discount Filter: koi bhi ek product pass kare to post. Jis product ka data
            # nahi mila uska discount pata nahi — wo pass nahi maana jaata.
            if md and live and all(_disc(p) < md for p in live):
                best = max(_disc(p) for p in live)
                await _edit_or_notify(wait_msg, notify, _low_discount_text(lang, best, md),
                                      parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                return
            status, detail, n = await post_amazon_original(
                context, uid, task, msg, raw_plain, raw_entities, amazon_urls, products, live, lang)
            if status == "posted":
                text = tr(lang, "✅ <b>Posted with the original caption!</b>",
                          "✅ <b>Original caption ke saath post ho gaya!</b>")
                if n:
                    text += f"\n🖼️ {esc(n)}"
                if live:
                    text += "\n" + _deal_info(max(live, key=_disc))
                else:
                    text += tr(lang, "\n⚠️ Amazon didn't return product details.",
                               "\n⚠️ Amazon se product details nahi mili.")
                    text += _nodata_reason(lang, products[0]["asin"])
                if md:
                    text += tr(lang, f"\n📉 Discount Filter {md}%+: ✅ passed",
                               f"\n📉 Discount Filter {md}%+: ✅ pass")
                text += footer
            elif status == "duplicate":
                text = tr(lang, f"♻️ <b>Already posted</b> ({esc(detail)} ago) — skipped.",
                          f"♻️ <b>Pehle post ho chuka hai</b> ({esc(detail)} pehle) — skip kiya.")
            else:
                text = tr(lang, "❌ <b>Post failed!</b>\n", "❌ <b>Post nahi hua!</b>\n") + esc(detail)
            await _edit_or_notify(wait_msg, notify, text, parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)
            return

        if products:
            cap = MAX_PER_MESSAGE if left is None else min(MAX_PER_MESSAGE, left)
            if len(products) > cap:
                await notify(tr(lang, f"⚠️ {len(products)} products found — taking the first {cap}.",
                                f"⚠️ {len(products)} products mile — pehle {cap} liye."))
                products = products[:cap]

            fetched = await get_products_by_asins([p["asin"] for p in products])
            posted, dupes, errors, posted_prods = [], [], [], []
            note, all_skipped = "", set()

            live = [fetched[p["asin"]] for p in products if p["asin"] in fetched]
            live.sort(key=lambda x: int(x.get("discount_pct") or 0), reverse=True)
            nodata = [p["asin"] for p in products if p["asin"] not in fetched]
            # Discount Filter (sirf Amazon): har product alag — kam discount wala skip
            low = [p for p in live if md and _disc(p) < md]
            live = [p for p in live if p not in low]

            if low and not live and not nodata:
                if len(low) == 1:
                    text = _low_discount_text(lang, _disc(low[0]), md, low[0].get("title") or "")
                else:
                    text = tr(lang, f"⏭️ <b>Skipped</b> — all {len(low)} deals have less than {md}% off.",
                              f"⏭️ <b>Skip</b> — saari {len(low)} deals ka discount {md}% se kam hai.")
                await _edit_or_notify(wait_msg, notify, text, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
                return

            for i, prod in enumerate(live):
                status, detail, n = await post_amazon_product(context, uid, task, prod, lang)
                if status == "posted":
                    posted.append(detail)
                    posted_prods.append(prod)
                    note = note or n
                    _, sk = build_amazon_caption(prod, make_affiliate_url(prod.get("asin", ""), tag),
                                                 cfg, has_image=bool(n))
                    all_skipped.update(sk)
                elif status == "duplicate":
                    dupes.append(detail)
                else:
                    errors.append(detail)
                if i < len(live) - 1:
                    await asyncio.sleep(POST_GAP_SECONDS)

            # Single product + data nahi mila:
            #  • Discount Filter ON → skip (discount pata hi nahi), duplicate mein nahi gina
            #  • Filter OFF → original text user ke tag ke saath (deal miss na ho)
            if len(products) == 1 and not posted and nodata and md:
                await _edit_or_notify(wait_msg, notify, _nodata_skip_text(lang, products[0]["asin"], md=md),
                                      parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                return

            # Single product + data nahi mila → original text user ke tag ke saath
            if len(products) == 1 and not posted and nodata:
                asin = products[0]["asin"]
                fb_key = ("a:" + asin) if cfg.get("dup_check", True) else ""
                if fb_key:
                    ok, when = claim_posted(uid, task["id"], fb_key)
                    if not ok:
                        await _edit_or_notify(wait_msg, notify,
                                              tr(lang, f"⚠️ <b>Already posted</b> ({when} ago) — skipped.",
                                                 f"⚠️ <b>Pehle post ho chuka hai</b> ({when} pehle) — skip kiya."),
                                              parse_mode=ParseMode.HTML)
                        return
                cp, ce = remove_footer(raw_plain, raw_entities)
                cp, ce = _strip_if_on(cp, ce, cfg)
                cp, ce = await replace_amazon_links(cp, ce, amazon_urls, tag)
                body   = entities_to_html(cp, ce, cfg.get("bold_links", True))
                try:
                    await deliver(context.bot.send_message,
                                  dict(chat_id=channel, text=wrap_plain_post(body, cfg, has_image=False),
                                       parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                       reply_markup=build_final_markup(cfg, asin=asin),
                                       disable_notification=cfg.get("silent", True)))
                    log_post(uid, task["id"], "amazon", asin, cp[:80])
                    await _edit_or_notify(
                        wait_msg, notify,
                        tr(lang,
                           "✅ <b>Posted!</b>\n⚠️ Amazon didn't return product details — sent your "
                           "original text with your affiliate link.",
                           "✅ <b>Post ho gaya!</b>\n⚠️ Amazon se product details nahi mili — aapka "
                           "original text aapke affiliate link ke saath bheja.")
                        + _nodata_reason(lang, asin) + footer,
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                except Exception as e:
                    release_posted(uid, task["id"], fb_key)
                    await _edit_or_notify(wait_msg, notify,
                                          tr(lang, "❌ <b>Post failed!</b>\n", "❌ <b>Post nahi hua!</b>\n")
                                          + esc(_friendly_error(e, lang)), parse_mode=ParseMode.HTML)
                return

            lines = []
            if len(posted) == 1 and not dupes and not nodata and not errors:
                lines.append(tr(lang, "✅ <b>Amazon deal posted!</b>", "✅ <b>Amazon deal post ho gayi!</b>"))
                info = _deal_info(posted_prods[0]) if posted_prods else ""
                if info:
                    lines.append(info)
                lines.append(f"🖼️ {note}" if note else tr(lang, "📝 Text post (no image)", "📝 Text post (photo nahi)"))
            else:
                lines.append(tr(lang, f"✅ <b>{len(posted)} deals posted!</b>",
                                f"✅ <b>{len(posted)} deal post ho gayi!</b>")
                             if posted else tr(lang, "⚠️ <b>No deal was posted.</b>",
                                               "⚠️ <b>Koi deal post nahi hui.</b>"))
                for i, t in enumerate(posted[:8]):
                    d = _disc(posted_prods[i]) if i < len(posted_prods) else 0
                    lines.append(f"   • {esc(t[:50])}" + (f" — <b>{d}%</b>" if d else ""))
                if note:
                    lines.append(f"🖼️ {note}")
            if dupes:
                lines.append(tr(lang, f"\n♻️ {len(dupes)} already posted in last 24h (skipped)",
                                f"\n♻️ {len(dupes)} pichle 24 ghante mein post ho chuki (skip)"))
            if nodata:
                lines.append(tr(lang, f"⏭️ {len(nodata)} skipped — Amazon returned no product details",
                                f"⏭️ {len(nodata)} skip — Amazon se product details nahi mili")
                             + _nodata_reason(lang, nodata[0])
                             + tr(lang, "\n<i>Send them again in a while — not counted as duplicate.</i>",
                                  "\n<i>Thodi der baad dobara bhejein — duplicate nahi maana jayega.</i>"))
            if errors:
                lines.append(f"❌ {len(errors)} — {esc(errors[0])}")
            if low:
                lines.append(tr(lang, f"📉 {len(low)} skipped — discount below {md}%:",
                                f"📉 {len(low)} skip — discount {md}% se kam:"))
                for p in low[:5]:
                    lines.append(f"   • {esc((p.get('title') or p.get('asin') or '')[:40])} — {_disc(p)}%")
            if searches:
                lines.append(tr(lang, f"🚫 {len(searches)} search pages skipped",
                                f"🚫 {len(searches)} search page chhod diye"))
            if md:
                lines.append(_filter_line(lang, md, passed=len(live), skipped=len(low)).lstrip("\n"))
            if all_skipped:
                names = ", ".join(FIELD_LABELS.get(k, k) for k in all_skipped)
                lines.append(tr(lang, f"\n✂️ Not enough space, left out: <b>{esc(names)}</b>",
                                f"\n✂️ Jagah kam thi, ye chhoot gaye: <b>{esc(names)}</b>"))
            await _edit_or_notify(wait_msg, notify, "\n".join(lines) + footer,
                                  parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return

        # ── Sirf unknown / search Amazon links ────────────────────────────
        # Offer / category / search page ka koi discount % nahi hota. Discount
        # Filter ON hai to ye pass nahi maane jaate → skip.
        if md:
            await _edit_or_notify(
                wait_msg, notify,
                tr(lang,
                   f"⏭️ <b>Skipped — no product in this post</b>\n"
                   f"🔗 Only Amazon offer / category / search pages — they have no discount %.\n"
                   f"📉 Discount Filter {md}%+ is ON, so not posted.\n"
                   "<i>To post such deals, turn the Discount Filter OFF in /tasks.</i>",
                   f"⏭️ <b>Skip — is post mein koi product nahi</b>\n"
                   f"🔗 Sirf Amazon offer / category / search page hain — inka discount % nahi hota.\n"
                   f"📉 Discount Filter {md}%+ ON hai, isliye post nahi ki.\n"
                   "<i>Aisi deals bhi post karni hain to /tasks mein Discount Filter OFF karein.</i>"),
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return
        cp, ce = remove_footer(raw_plain, raw_entities)
        cp, ce = _strip_if_on(cp, ce, cfg)
        cp, ce = await replace_amazon_links(cp, ce, amazon_urls, tag)
        payload = _msg_payload(msg, cp, ce)
        status, detail = await post_other(context, uid, task, payload, lang, wm_kind="amazon")
        await _report_other(wait_msg, notify, status, detail, lang, footer, tagged=True)
        return

    # ==========================================================================
    # NON-AMAZON
    # ==========================================================================
    fresh = await _still_running(task, uid, lang, tname, None, notify)
    if fresh is None:
        return
    task, cfg = fresh, fresh["cfg"]
    cp, ce  = remove_footer(raw_plain, raw_entities)
    # Doosre channel ka @username / Telegram link hatao (Flipkart, Myntra... sab posts)
    cp, ce  = _strip_if_on(cp, ce, cfg)
    payload = _msg_payload(msg, cp, ce)
    status, detail = await post_other(context, uid, task, payload, lang)
    await _report_other(None, notify, status, detail, lang, footer_plain)


async def _still_running(task: dict, uid: int, lang: str, tname: str, wait_msg, notify):
    """Post bhejne se theek pehle taaza task. Beech mein user ne task DELETE ya
    PAUSE kar diya ho to None (aur Draft mein wajah). Warna taaza task."""
    fresh = get_task(task["id"], uid)
    if fresh is None:
        await _edit_or_notify(wait_msg, notify,
                              tr(lang, f"🗑️ <b>Skipped</b> — task {esc(tname)} was deleted.",
                                 f"🗑️ <b>Skip</b> — task {esc(tname)} delete ho chuka hai."),
                              parse_mode=ParseMode.HTML)
        return None
    if fresh.get("paused"):
        await _edit_or_notify(wait_msg, notify,
                              tr(lang, f"⏸️ <b>Skipped</b> — {esc(tname)} is paused.",
                                 f"⏸️ <b>Skip</b> — {esc(tname)} pause hai."),
                              parse_mode=ParseMode.HTML)
        return None
    return fresh


def _min_discount(cfg: dict) -> int:
    """Task ka Discount Filter (0 = OFF)."""
    try:
        v = int(cfg.get("min_discount") or 0)
    except (TypeError, ValueError):
        return 0
    return v if 0 < v < 100 else 0


def _disc(product: dict) -> int:
    try:
        return int(product.get("discount_pct") or 0)
    except (TypeError, ValueError):
        return 0


def _deal_info(p: dict) -> str:
    """Draft reply ke liye: '🛍️ naam\n💰 ₹199 (MRP ₹850) · 77% off'."""
    out = []
    title = (p.get("title") or p.get("asin") or "").strip()
    if title:
        out.append(f"🛍️ {esc(title[:70])}")
    price, mrp, d = p.get("deal_price") or "", p.get("actual_price") or "", _disc(p)
    bits = []
    if price:
        bits.append(f"<b>{esc(price)}</b>" + (f" (MRP {esc(mrp)})" if mrp and mrp != price else ""))
    if d:
        bits.append(f"<b>{d}% off</b>")
    if bits:
        out.append("💰 " + " · ".join(bits))
    return "\n".join(out)


def _nodata_skip_text(lang: str, asin: str, n: int = 1, md: int = 0) -> str:
    """Discount Filter ON + Amazon se detail nahi mili → post nahi hui. Draft mein saaf wajah."""
    what = f"<code>{esc(asin)}</code>" + (f" (+{n - 1})" if n > 1 else "")
    return (tr(lang,
               f"⏭️ <b>Skipped — Amazon didn't return product details</b>\n🛍️ {what}",
               f"⏭️ <b>Skip — Amazon se product details nahi mili</b>\n🛍️ {what}")
            + _nodata_reason(lang, asin)
            + tr(lang,
                 f"\n📉 Discount Filter {md}%+ is ON — discount unknown, so not posted.",
                 f"\n📉 Discount Filter {md}%+ ON hai — discount pata nahi chala, isliye post nahi ki.")
            + tr(lang,
                 "\n<i>Send it again in a while — it won't be counted as a duplicate.</i>",
                 "\n<i>Thodi der baad dobara bhejein — duplicate nahi maana jayega.</i>"))


def _filter_line(lang: str, md: int, passed: int = 0, skipped: int = 0) -> str:
    """Discount Filter ON ho to reply mein saaf likho ki kya hua."""
    if not md:
        return ""
    parts = []
    if passed:
        parts.append(tr(lang, f"✅ {passed} passed", f"✅ {passed} pass"))
    if skipped:
        parts.append(tr(lang, f"⏭️ {skipped} skipped", f"⏭️ {skipped} skip"))
    return f"\n📉 Discount Filter {md}%+: " + (" · ".join(parts) or "—")


def _low_discount_text(lang: str, have: int, need: int, title: str = "") -> str:
    name = f"\n🛍️ {esc(title[:60])}" if title else ""
    return tr(lang,
              f"⏭️ <b>Skipped — discount too low</b>{name}\n📉 Discount: <b>{have}%</b>  "
              f"(filter: {need}%+)\n<i>Change it in /tasks → 📉 Discount Filter.</i>",
              f"⏭️ <b>Skip — discount kam hai</b>{name}\n📉 Discount: <b>{have}%</b>  "
              f"(filter: {need}%+)\n<i>/tasks → 📉 Discount Filter se badal sakte hain.</i>")


async def _report_other(wait_msg, notify, status, detail, lang, footer, tagged=False):
    if status == "posted":
        text = tr(lang, "✅ <b>Posted!</b>", "✅ <b>Post ho gaya!</b>")
        if tagged:
            text += tr(lang, "\n<i>Your affiliate tag was added to the link.</i>",
                       "\n<i>Link pe aapka affiliate tag laga diya.</i>")
        text += footer
    elif status == "duplicate":
        text = tr(lang, f"♻️ <b>Already posted</b> ({esc(detail)}) — skipped.",
                  f"♻️ <b>Pehle post ho chuka hai</b> ({esc(detail)}) — skip kiya.")
    else:
        text = tr(lang, "❌ <b>Post failed!</b>\n", "❌ <b>Post nahi hua!</b>\n") + esc(detail)
    await _edit_or_notify(wait_msg, notify, text, parse_mode=ParseMode.HTML,
                          disable_web_page_preview=True)


def task_name(task: dict, lang: str = "hi") -> str:
    name = (task.get("cfg", {}).get("name") or "").strip()
    return name or f"Task #{task.get('id')}"

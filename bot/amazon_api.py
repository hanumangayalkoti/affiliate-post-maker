"""
amazon_api.py — Amazon Creators API (sirf owner ki credentials), product cache
aur har user ke apne affiliate tag wale links.

API se data owner ke account se aata hai, lekin post ke links mein hamesha
USER ka tag lagta hai. Same product baar-baar aaye to cache se data milta hai,
taaki API limit bachi rahe.
"""
import os
import re
import time
import asyncio
import logging
import aiohttp
import ipaddress
import urllib.parse
from collections import OrderedDict
from datetime import datetime, timedelta

from database import cache_get, cache_put_many

logger = logging.getLogger(__name__)

CREDENTIAL_ID      = os.getenv("CREDENTIAL_ID", "")
CREDENTIAL_SECRET  = os.getenv("CREDENTIAL_SECRET", "")
CREDENTIAL_VERSION = os.getenv("CREDENTIAL_VERSION", "3.2")
MARKETPLACE        = os.getenv("MARKETPLACE", "www.amazon.in")

# Owner ka tag — sirf API request ke liye. Post links mein user ka tag lagta hai.
PARTNER_TAG = os.getenv("PARTNER_TAG", "")
if not PARTNER_TAG:
    logger.warning("PARTNER_TAG env var set nahi hai! Amazon API kaam nahi karegi.")

# Cache: itne minute tak same product ka data dobara API se nahi mangenge
CACHE_FRESH_MINUTES = float(os.getenv("CACHE_FRESH_MINUTES", "30"))
# API fail ho jaye to itne ghante purana data bhi chalega
STALE_FALLBACK_HOURS = float(os.getenv("STALE_FALLBACK_HOURS", "6"))
# Do API calls ke beech kam se kam itna gap (Amazon ki TPS limit)
API_MIN_GAP_SECONDS = float(os.getenv("API_MIN_GAP_SECONDS", "1.1"))

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,60}-\d{2}$")

_api_lock = asyncio.Lock()
_last_call_at = 0.0
_api_stats = {"calls": 0, "cache_hits": 0, "asins_fetched": 0}

VERSION_TOKEN_URLS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}

SCOPE    = "creatorsapi::default" if CREDENTIAL_VERSION.startswith("3.") else "creatorsapi/default"
API_BASE = "https://creatorsapi.amazon"
ITEMS_EP = f"{API_BASE}/catalog/v1/getItems"

# Ek call mein max 10 ASIN — Amazon ki limit
MAX_ASINS_PER_CALL = 10

ASIN_PAT = re.compile(r"/(?:dp|gp/product|exec/obidos/ASIN|o/ASIN)/([A-Za-z0-9]{10})")

NEEDS_REDIRECT = ("amzn.to", "amzn.in", "amzn.eu", "amzn.asia", "a.co", "link.amazon")

SEARCH_MARKERS = (
    "/s?", "/s/", "/search",
    "field-keywords", "keywords=", "k=",
    "/b?", "/b/", "node=",
    "/deals", "/gp/goldbox", "/goldbox",
    "/gp/browse", "/gp/search",
    "/gp/bestsellers", "/bestsellers", "/gp/new-releases", "/gp/movers-and-shakers",
    "/stores/", "/shop/", "/brand/",
    "/gcx/", "/events/", "/promotion", "/hz/",
    "/gp/most-wished-for", "/international-shopping",
)

_token_cache: dict = {"token": None, "expires_at": None}

PRODUCT_RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "itemInfo.title",
    "itemInfo.features",
    "itemInfo.byLineInfo",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.dealDetails",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.isBuyBoxWinner",
    "browseNodeInfo.websiteSalesRank",
    "browseNodeInfo.browseNodes",
    "customerReviews.count",
    "customerReviews.starRating",
]


async def _get_token() -> str | None:
    now = datetime.now()
    if _token_cache["token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not CREDENTIAL_ID or not CREDENTIAL_SECRET:
        logger.error("CREDENTIAL_ID ya CREDENTIAL_SECRET set nahi hai")
        return None

    token_url = VERSION_TOKEN_URLS.get(CREDENTIAL_VERSION)
    if not token_url:
        logger.error(f"Unsupported CREDENTIAL_VERSION: {CREDENTIAL_VERSION}")
        return None

    is_lwa = CREDENTIAL_VERSION.startswith("3.")
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     CREDENTIAL_ID,
        "client_secret": CREDENTIAL_SECRET,
        "scope":         SCOPE,
    }

    try:
        async with aiohttp.ClientSession() as session:
            if is_lwa:
                req = session.post(
                    token_url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            else:
                req = session.post(
                    token_url, data=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            async with req as resp:
                if resp.status == 200:
                    data       = await resp.json()
                    token      = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    _token_cache["token"]      = token
                    _token_cache["expires_at"] = now + timedelta(seconds=expires_in - 60)
                    logger.info("Amazon Creators API token mila!")
                    return token
                body = await resp.text()
                logger.error(f"Token error {resp.status}: {body[:300]}")
                return None
    except Exception as e:
        logger.error(f"Token fetch fail: {e}")
        return None


# =============================================================================
# URL HELPERS
# =============================================================================
def extract_asin(url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", url):
        return url.upper()
    m = ASIN_PAT.search(url)
    if m:
        return m.group(1).upper()
    q = re.search(r"[?&](?:ASIN|asin|ASIN\.1)=([A-Za-z0-9]{10})", url)
    if q:
        return q.group(1).upper()
    try:
        path = urllib.parse.urlparse(url).path.strip("/")
    except Exception:
        return None
    if re.fullmatch(r"[A-Za-z0-9]{10}", path):
        return path.upper()
    return None


_AMAZON_HOST_RE = re.compile(
    r"(?:^|\.)amazon\.(?:in|com|co\.uk|de|fr|it|es|ca|co\.jp|com\.au|com\.br|com\.mx|ae|sa|sg|nl|"
    r"se|pl|com\.tr|com\.be|eg)$"
)
_SHORT_HOSTS = {"amzn.to", "amzn.in", "amzn.eu", "amzn.asia", "a.co"}


def _host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split("@")[-1].split(":")[0].strip(".")
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_amazon_url(url: str) -> bool:
    """Sirf asli Amazon domains — amazon.evil.com jaise fake domain nahi."""
    host = _host(url)
    if not host:
        return False
    return (host in _SHORT_HOSTS or bool(_AMAZON_HOST_RE.search(host))
            or host == "amazon" or host.endswith(".amazon"))


def is_amazon_search_url(url: str) -> bool:
    """Search/browse/deals page hai ya nahi. Caller pehle extract_asin() try kare."""
    if not url:
        return False
    low = url.lower()
    try:
        parsed = urllib.parse.urlparse(low)
        path   = parsed.path or ""
        query  = parsed.query or ""
    except Exception:
        path, query = low, ""

    for marker in SEARCH_MARKERS:
        if marker.endswith("="):
            if re.search(r"[?&]" + re.escape(marker), "?" + query):
                return True
        elif marker.startswith("/"):
            if marker.rstrip("?") in path or marker in low:
                return True
        elif marker in low:
            return True
    return False


def needs_redirect(url: str) -> bool:
    host = _host(url)
    return host in _SHORT_HOSTS or host.endswith(".amazon") or host == "amazon"


def _strip_tag_param(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params.pop("tag", None)
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(params, doseq=True))
        )
    except Exception:
        return url


def is_valid_tag(tag: str) -> bool:
    """Amazon tag aisa dikhta hai: mytag-21"""
    return bool(TAG_RE.match((tag or "").strip()))


def make_affiliate_url(asin: str, tag: str) -> str:
    base = f"https://{MARKETPLACE}/dp/{asin}"
    return f"{base}?tag={tag}" if tag else base


def make_cart_url(asin: str, tag: str) -> str:
    """
    Add-to-Cart link. Cart mein daalne se attribution window
    24 ghante se 89 din tak badh jaati hai.
    """
    if not asin:
        return ""
    url = f"https://{MARKETPLACE}/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=1"
    if tag:
        url += f"&AssociateTag={urllib.parse.quote(tag)}"
    return url


async def _resolve_redirect(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


# =============================================================================
# THIRD-PARTY SHORT LINKS (amzn-to.co, bit.ly, ...) — peeche chhupa Amazon link
# =============================================================================
# Kai deal channel apne shortener use karte hain (jaise https://amzn-to.co/AZR77S).
# Domain Amazon ka nahi hota, isliye pehle bot inhe pehchaan hi nahi paata tha.
# Ab aise link ko kholke dekhte hain: redirect follow karte hain, aur agar
# shortener beech mein HTML page dikhata hai to page ke andar Amazon link
# (meta refresh / JavaScript / href) dhoondhte hain.

# In domains ke peeche Amazon kabhi nahi hota — inhe kholna time ki barbaadi hai.
_NEVER_AMAZON_HOSTS = (
    "t.me", "telegram.me", "telegram.org", "wa.me", "whatsapp.com",
    "youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.com",
    "twitter.com", "x.com", "google.com", "goo.gl", "play.google.com",
    "flipkart.com", "fkrt.it", "fkrt.cc", "myntra.com", "myntr.it",
    "ajio.com", "ajiio.in", "meesho.com", "nykaa.com", "tatacliq.com",
    "jiomart.com", "shopsy.in", "croma.com", "snapdeal.com",
)
_MAX_HOPS        = 8
_MAX_BODY_BYTES  = 300_000
_UNSHORT_TIMEOUT = 10
_UNSHORT_CACHE_MAX = 3000
# url -> mila hua Amazon link ("" = iske peeche Amazon nahi hai)
_unshort_cache: "OrderedDict[str, str]" = OrderedDict()

_HTML_AMAZON_RE = re.compile(
    r"https?:(?:\\?/){2}(?:[a-z0-9-]+\.)*(?:amazon\.[a-z.]{2,10}|amzn\.(?:to|in|eu|asia)|a\.co)"
    r"(?:\\?/[^\s\"'<>()]*)?",
    re.IGNORECASE,
)
_META_REFRESH_RE = re.compile(
    r"<meta[^>]+http-equiv=[\"']?refresh[^>]*content=[\"']?\s*\d*\s*;?\s*url=([^\"'>\s]+)",
    re.IGNORECASE,
)
_JS_LOCATION_RE = re.compile(
    r"(?:window\.|document\.|top\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _cache_unshort(url: str, value: str) -> None:
    _unshort_cache[url] = value
    _unshort_cache.move_to_end(url)
    while len(_unshort_cache) > _UNSHORT_CACHE_MAX:
        _unshort_cache.popitem(last=False)


def cached_amazon_target(url: str) -> str:
    """Pehle se pata hai ki is link ke peeche Amazon hai? To wo link, warna ""."""
    return _unshort_cache.get(url, "")


def is_known_amazon(url: str) -> bool:
    """Asli Amazon link, YA aisa short link jiske peeche Amazon mil chuka hai."""
    return is_amazon_url(url) or bool(cached_amazon_target(url))


def _is_public_host(host: str) -> bool:
    """Server ke andar ke address (localhost, 10.x, *.internal) kabhi mat kholo."""
    if not host or "." not in host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return True
    return ip.is_global


def _never_amazon(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _NEVER_AMAZON_HOSTS)


def _clean_found_url(u: str) -> str:
    u = u.replace("\\/", "/").replace("&amp;", "&").replace("\\u0026", "&")
    return u.rstrip(".,;)")


def _amazon_url_in_html(body: str) -> str:
    """Page mein jitne Amazon link hain, unme se ASIN wala pehle, warna pehla."""
    found = [_clean_found_url(m.group(0)) for m in _HTML_AMAZON_RE.finditer(body)]
    found = [u for u in found if is_amazon_url(u)]
    for u in found:
        if extract_asin(u):
            return u
    for u in found:
        if needs_redirect(u):
            return u
    return ""


def _next_hop_in_html(body: str, base: str) -> str:
    """Meta refresh ya JavaScript redirect wala agla link (Amazon na ho tab bhi)."""
    for rx in (_META_REFRESH_RE, _JS_LOCATION_RE):
        m = rx.search(body)
        if m:
            return urllib.parse.urljoin(base, _clean_found_url(m.group(1).strip()))
    return ""


async def find_amazon_behind(url: str) -> str:
    """
    Non-Amazon dikhne wale link ke peeche Amazon product hai to wo Amazon link
    lautao, warna "". Result cache hota hai, isliye same link dobara nahi khulta.
    """
    if not url:
        return ""
    if is_amazon_url(url):
        return url
    if url in _unshort_cache:
        return _unshort_cache[url]

    found = ""
    cur = url
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_UNSHORT_TIMEOUT),
            headers={
                "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        ) as session:
            for _ in range(_MAX_HOPS):
                if is_amazon_url(cur):
                    found = cur
                    break
                host = _host(cur)
                if urllib.parse.urlparse(cur).scheme not in ("http", "https"):
                    break
                if not _is_public_host(host) or _never_amazon(host):
                    break
                async with session.get(cur, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        cur = urllib.parse.urljoin(cur, location)
                        continue
                    if "html" not in (resp.headers.get("Content-Type") or "").lower():
                        break
                    raw = await resp.content.read(_MAX_BODY_BYTES)
                    body = raw.decode(resp.charset or "utf-8", errors="ignore")
                inside = _amazon_url_in_html(body)
                if inside:
                    found = inside
                    break
                nxt = _next_hop_in_html(body, cur)
                if not nxt or nxt == cur:
                    break
                cur = nxt
    except Exception as e:
        logger.info(f"Short link check fail ({url[:60]}): {e}")
        # Network ki gadbad pe "" cache nahi karte — agli baar phir try hoga
        return ""

    if found and not extract_asin(found) and needs_redirect(found):
        found = await _resolve_redirect(found)
        if not is_amazon_url(found):
            found = ""
    _cache_unshort(url, found)
    if found:
        logger.info(f"Short link {url[:60]} -> {found[:100]}")
    return found


async def find_amazon_behind_many(urls: list, limit: int = 10) -> list:
    """Message ke non-Amazon links mein se jinke peeche Amazon hai (saath-saath check)."""
    todo = []
    for u in urls:
        if u and u not in todo and not is_amazon_url(u):
            todo.append(u)
    todo = todo[:limit]
    if not todo:
        return []
    results = await asyncio.gather(*(find_amazon_behind(u) for u in todo), return_exceptions=True)
    return [u for u, r in zip(todo, results) if isinstance(r, str) and r]


async def resolve_amazon_url(url: str) -> str:
    target = cached_amazon_target(url)
    if target:
        url = target
    if needs_redirect(url):
        return await _resolve_redirect(url)
    return url


async def get_short_affiliate_link(url: str, tag: str) -> str:
    """Kisi bhi Amazon link pe user ka tag lagao (purana tag hata ke)."""
    if not is_amazon_url(url):
        url = cached_amazon_target(url) or await find_amazon_behind(url) or url
    asin = extract_asin(url)
    resolved = url
    if not asin and needs_redirect(url):
        resolved = await _resolve_redirect(url)
        asin = extract_asin(resolved)
    if asin:
        return make_affiliate_url(asin, tag)
    cleaned = _strip_tag_param(resolved)
    if tag:
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}tag={tag}"
    return cleaned


# =============================================================================
# RESPONSE PARSING
# =============================================================================
def inr(amount) -> str:
    """₹ Indian style: 1249999 → ₹12,49,999 (paise sirf tab jab hon)."""
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return ""
    whole = int(v)
    paise = round((v - whole) * 100)
    if paise == 100:
        whole, paise = whole + 1, 0
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"₹{s}" + (f".{paise:02d}" if paise else "")


def _parse_item(item: dict) -> dict:
    r: dict = {}

    info = item.get("itemInfo") or {}

    title_data = info.get("title") or {}
    r["title"] = (title_data.get("displayValue") or "").strip()

    img_primary = (item.get("images") or {}).get("primary") or {}
    img = (img_primary.get("large") or img_primary.get("medium")
           or img_primary.get("small") or {})
    r["image_url"] = (img or {}).get("url", "") or ""

    # ── Brand ──────────────────────────────────────────────────────────────
    byline = info.get("byLineInfo") or {}
    brand  = (byline.get("brand") or {}).get("displayValue", "")
    if not brand:
        brand = (byline.get("manufacturer") or {}).get("displayValue", "")
    r["brand"] = (brand or "").strip()

    # ── Features ───────────────────────────────────────────────────────────
    feats = (info.get("features") or {}).get("displayValues") or []
    r["features"] = [f for f in feats if f]

    # ── Price block ────────────────────────────────────────────────────────
    r["deal_price"]    = ""
    r["actual_price"]  = ""
    r["deal_amount"]   = 0.0
    r["actual_amount"] = 0.0
    r["discount_pct"]  = 0
    r["savings"]       = ""
    r["stock_note"]    = ""
    r["seller"]        = ""
    r["deal_badge"]    = ""
    r["deal_ends"]     = ""

    listings = (item.get("offersV2") or {}).get("listings") or []
    if listings:
        listing   = listings[0]
        price_obj = listing.get("price") or {}
        money     = price_obj.get("money") or {}
        if money:
            r["deal_price"]  = money.get("displayAmount", "") or ""
            try:
                r["deal_amount"] = float(money.get("amount", 0) or 0)
            except (TypeError, ValueError):
                r["deal_amount"] = 0.0
            if r["deal_amount"]:
                r["deal_price"] = inr(r["deal_amount"])

        savings_obj = price_obj.get("savings") or {}
        sav_money   = savings_obj.get("money") or {}
        if sav_money:
            try:
                sav_amt = float(sav_money.get("amount", 0) or 0)
            except (TypeError, ValueError):
                sav_amt = 0.0
            r["savings"] = sav_money.get("displayAmount", "") or ""
            if sav_amt:
                r["savings"] = inr(sav_amt) or r["savings"]
            if sav_amt and r["deal_amount"]:
                mrp = r["deal_amount"] + sav_amt
                r["actual_amount"] = mrp
                r["actual_price"]  = inr(mrp)

        pct = savings_obj.get("percentage")
        if pct is not None:
            try:
                r["discount_pct"] = int(pct)
            except (TypeError, ValueError):
                pass
        elif r["deal_amount"] and r["actual_amount"]:
            try:
                r["discount_pct"] = round(
                    (r["actual_amount"] - r["deal_amount"]) / r["actual_amount"] * 100
                )
            except Exception:
                pass

        # ── Stock ──────────────────────────────────────────────────────────
        avail = listing.get("availability") or {}
        msg   = (avail.get("message") or "").strip()
        maxq  = avail.get("maxOrderQuantity")
        if msg:
            r["stock_note"] = msg
        elif isinstance(maxq, int) and 0 < maxq <= 10:
            r["stock_note"] = f"Sirf {maxq} bache hain"

        # ── Seller ─────────────────────────────────────────────────────────
        merch = listing.get("merchantInfo") or {}
        r["seller"] = (merch.get("name") or "").strip()

        # ── Deal details ───────────────────────────────────────────────────
        deal = listing.get("dealDetails") or {}
        badge = (deal.get("badge") or deal.get("dealBadge")
                 or deal.get("accessType") or "")
        if isinstance(badge, dict):
            badge = badge.get("displayValue", "") or badge.get("label", "")
        badge = str(badge or "").replace("_", " ").strip()
        if badge:
            r["deal_badge"] = badge.title() if badge.isupper() else badge

        ends = deal.get("endTime") or deal.get("endDate") or ""
        if ends:
            r["deal_ends"] = _humanise_deal_end(str(ends))

        if deal.get("percentClaimed") is not None and not r["deal_ends"]:
            try:
                claimed = int(deal["percentClaimed"])
                if claimed > 0:
                    r["deal_ends"] = f"{claimed}% claimed"
            except (TypeError, ValueError):
                pass

    # ── Sales rank ─────────────────────────────────────────────────────────
    bni  = item.get("browseNodeInfo") or {}
    rank = bni.get("websiteSalesRank") or {}
    r["sales_rank"] = ""
    if rank:
        num = rank.get("salesRank")
        cat = (rank.get("contextFreeName") or rank.get("displayName") or "").strip()
        if num and cat:
            r["sales_rank"] = f"#{num:,} in {cat}"

    nodes = bni.get("browseNodes") or []
    r["category"] = ""
    if nodes:
        r["category"] = (nodes[0].get("contextFreeName")
                         or nodes[0].get("displayName") or "").strip()

    # ── Reviews ────────────────────────────────────────────────────────────
    cr   = item.get("customerReviews") or {}
    star = cr.get("starRating") or {}
    r["rating"] = str(star.get("value", "")).strip() if star else ""
    count       = cr.get("count")
    r["review_count"] = f"{count:,}" if isinstance(count, int) else str(count or "")

    # ── Amazon ka apna tagged link (hamare banaye se behtar) ───────────────
    r["detail_url"] = item.get("detailPageURL", "") or ""

    return r


def _humanise_deal_end(raw: str) -> str:
    """ISO timestamp ko 'X ghante baaki' mein badlo."""
    try:
        cleaned = raw.replace("Z", "+00:00")
        end     = datetime.fromisoformat(cleaned)
        if end.tzinfo:
            from datetime import timezone
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()
        secs = (end - now).total_seconds()
        if secs <= 0:
            return "khatam"
        hours = int(secs // 3600)
        mins  = int((secs % 3600) // 60)
        if hours >= 1:
            return f"{hours} ghante baaki"
        return f"{mins} minute baaki"
    except Exception:
        return ""


# =============================================================================
# API CALLS
# =============================================================================
async def _throttle():
    """Do API calls ke beech gap rakho — saare users ek hi API share karte hain."""
    global _last_call_at
    wait = API_MIN_GAP_SECONDS - (time.monotonic() - _last_call_at)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_call_at = time.monotonic()


async def _call_get_items(asins: list) -> dict:
    """Ek call, max 10 ASIN. Returns {asin: parsed_product} (bina links ke)."""
    token = await _get_token()
    if not token:
        return {}
    await _throttle()
    _api_stats["calls"] += 1

    payload = {
        "itemIds":    asins,
        "itemIdType": "ASIN",
        "marketplace": MARKETPLACE,
        "partnerTag": PARTNER_TAG,
        "resources":  PRODUCT_RESOURCES,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ITEMS_EP, json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-marketplace": MARKETPLACE,
                    "Content-Type":  "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status == 403:
                    _token_cache["token"]      = None
                    _token_cache["expires_at"] = None
                    logger.error("Amazon API 403 — token invalidated")
                    return {}
                if resp.status not in (200, 206):
                    body = await resp.text()
                    logger.error(f"GetItems {resp.status}: {body[:300]}")
                    return {}
                data = await resp.json()
    except Exception as e:
        logger.error(f"GetItems call fail: {e}")
        return {}

    # Docs do naam dikhate hain — dono accept karo, warna chup-chaap fail hoga
    container = data.get("itemsResult") or data.get("itemResults") or {}
    items     = container.get("items") or []

    out = {}
    for item in items:
        asin = (item.get("asin") or "").upper()
        if not asin:
            continue
        parsed = _parse_item(item)
        parsed["asin"] = asin
        # Amazon ka detailPageURL owner ke tag wala hai — kabhi use nahi karna.
        parsed.pop("detail_url", None)
        out[asin] = parsed

    for err in (data.get("errors") or []):
        logger.warning(f"GetItems error: {err.get('code')} — {err.get('message', '')[:120]}")

    return out


async def get_products_by_asins(asins: list, max_age_minutes: float = None,
                                allow_stale: bool = True) -> dict:
    """
    Kai ASIN ka data lao. Pehle cache dekho, jo fresh nahi hai sirf wahi API se
    (10-10 ke batch mein). Returns {asin: product} — product mein links NAHI hote,
    wo har user ke tag se alag banate hain.
    """
    if max_age_minutes is None:
        max_age_minutes = CACHE_FRESH_MINUTES

    uniq, seen = [], set()
    for a in asins:
        a = (a or "").upper()
        if a and a not in seen:
            seen.add(a)
            uniq.append(a)
    if not uniq:
        return {}

    result = {}
    fresh = cache_get(uniq, max_age_minutes)
    for a, (prod, _) in fresh.items():
        result[a] = dict(prod)
    _api_stats["cache_hits"] += len(fresh)

    missing = [a for a in uniq if a not in result]
    if missing:
        fetched = {}
        async with _api_lock:
            # Lock ke intezaar mein kisi aur ne shayad la diya ho
            again = cache_get(missing, max_age_minutes)
            for a, (prod, _) in again.items():
                result[a] = dict(prod)
            missing = [a for a in missing if a not in result]
            for i in range(0, len(missing), MAX_ASINS_PER_CALL):
                chunk = missing[i:i + MAX_ASINS_PER_CALL]
                fetched.update(await _call_get_items(chunk))
        if fetched:
            cache_put_many(fetched)
            _api_stats["asins_fetched"] += len(fetched)
            result.update({a: dict(p) for a, p in fetched.items()})

        # API ne nahi diya — thoda purana cache bhi chalega
        still = [a for a in missing if a not in fetched]
        if still and allow_stale:
            stale = cache_get(still, STALE_FALLBACK_HOURS * 60)
            for a, (prod, _) in stale.items():
                result[a] = dict(prod)

    logger.info(f"Amazon: {len(uniq)} ASIN maange — cache {len(fresh)}, "
                f"API {len(missing)}, mile {len(result)}")
    return result


async def get_product_by_asin(asin: str, max_age_minutes: float = None,
                              allow_stale: bool = True) -> dict | None:
    got = await get_products_by_asins([asin], max_age_minutes, allow_stale)
    return got.get((asin or "").upper())


def api_stats() -> dict:
    return dict(_api_stats)

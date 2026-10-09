"""
store_ids.py — Non-Amazon link ke peeche ka product number (Amazon ke ASIN jaisa).

EarnKaro jaise bot har baar NAYA chhota link dete hain (myntr.it/xxx), isliye
link se duplicate nahi pakda jaata. Link kholke asli product page tak jaate hain
aur wahan se product number nikalte hain:

    Myntra   myntra.com/.../31076617/buy        → "myntra:31076617"
    Flipkart flipkart.com/...?pid=MOBH4DQFG8NKF  → "fk:MOBH4DQFG8NKF"
    Shopsy   shopsy.in/...?pid=WNUHK7HRQP4KDDHR  → "fk:WNUHK7HRQP4KDDHR"
             (Shopsy Flipkart ki hi hai — same product ka pid same hota hai)
"""
import asyncio
import logging
import re
import urllib.parse
from collections import OrderedDict

import aiohttp

from amazon_api import _host, _is_public_host

logger = logging.getLogger(__name__)

_MAX_HOPS = 8
_MAX_BODY = 300_000
_TIMEOUT = 10
_CACHE_MAX = 3000
_cache: "OrderedDict[str, str]" = OrderedDict()     # url -> product key ("" = nahi mila)

_MYNTRA_RE = re.compile(r"/(\d{5,12})(?:/buy)?/?$")
_PID_RE = re.compile(r"^[A-Z0-9]{10,20}$")
_URL_IN_HTML_RE = re.compile(
    r"https?:(?:\\?/){2}(?:www\.|m\.|dl\.)?(?:myntra\.com|flipkart\.com|shopsy\.in)[^\s\"'<>]*",
    re.I)
_META_REFRESH_RE = re.compile(r"""http-equiv=["']?refresh["']?[^>]*content=["']?\d+\s*;\s*url=([^"'>\s]+)""", re.I)
_JS_LOCATION_RE = re.compile(r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""", re.I)


def _is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def store_product_key(url: str, _depth: int = 0) -> str:
    """Link mein hi product number ho to wo (bina internet ke). Warna ""."""
    if not url or _depth > 2:
        return ""
    try:
        p = urllib.parse.urlparse(url.replace("&amp;", "&"))
    except Exception:
        return ""
    host = _host(url)
    qs = urllib.parse.parse_qs(p.query)
    if _is(host, "myntra.com"):
        m = _MYNTRA_RE.search(p.path.rstrip("/"))
        if m:
            return "myntra:" + m.group(1)
    if _is(host, "flipkart.com") or _is(host, "shopsy.in"):
        pid = (qs.get("pid") or [""])[0].strip().upper()
        if _PID_RE.match(pid):
            return "fk:" + pid
    # Tracking link (EarnKaro / affiliate redirect) ke andar asli link chhupa ho
    for values in qs.values():
        for v in values:
            v = urllib.parse.unquote(v)
            if v.startswith(("http://", "https://")):
                key = store_product_key(v, _depth + 1)
                if key:
                    return key
    return ""


def _key_in_html(body: str) -> str:
    for m in _URL_IN_HTML_RE.finditer(body):
        u = m.group(0).replace("\\/", "/").replace("&amp;", "&").replace("\\u0026", "&")
        key = store_product_key(u)
        if key:
            return key
    return ""


def _next_hop(body: str, base: str) -> str:
    for rx in (_META_REFRESH_RE, _JS_LOCATION_RE):
        m = rx.search(body)
        if m:
            return urllib.parse.urljoin(base, m.group(1).strip().replace("&amp;", "&"))
    return ""


def _remember(url: str, key: str) -> None:
    _cache[url] = key
    _cache.move_to_end(url)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


async def find_product_key(url: str) -> str:
    """Link (chhota / tracking) kholke product number. "" = nahi mila ya link nahi khula."""
    if not url:
        return ""
    key = store_product_key(url)
    if key:
        return key
    if url in _cache:
        return _cache[url]
    cur = url
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            headers={
                "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        ) as session:
            for _ in range(_MAX_HOPS):
                key = store_product_key(cur)
                if key:
                    break
                if urllib.parse.urlparse(cur).scheme not in ("http", "https"):
                    break
                if not _is_public_host(_host(cur)):
                    break
                async with session.get(cur, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if resp.status in (301, 302, 303, 307, 308) and location:
                        cur = urllib.parse.urljoin(cur, location)
                        continue
                    if "html" not in (resp.headers.get("Content-Type") or "").lower():
                        break
                    raw = await resp.content.read(_MAX_BODY)
                    body = raw.decode(resp.charset or "utf-8", errors="ignore")
                key = _key_in_html(body)
                if key:
                    break
                nxt = _next_hop(body, cur)
                if not nxt or nxt == cur:
                    break
                cur = nxt
    except Exception as e:
        logger.info(f"Product link check fail ({url[:60]}): {e}")
        return ""                       # network gadbad — cache nahi, agli baar phir try
    _remember(url, key)
    if key:
        logger.info(f"Product link {url[:60]} -> {key}")
    return key


async def find_product_keys(urls: list, limit: int = 5) -> list:
    """Post ke links mein se jitne product mile (saath-saath check, max `limit`)."""
    todo = list(dict.fromkeys(u for u in urls if u))[:limit]
    if not todo:
        return []
    got = await asyncio.gather(*(find_product_key(u) for u in todo), return_exceptions=True)
    return list(dict.fromkeys(k for k in got if isinstance(k, str) and k))

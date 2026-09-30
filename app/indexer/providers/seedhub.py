"""Low-frequency SeedHub search adapter.

The public integrations use ``/s/<query>/?page=1`` and parse ``.cover`` cards.
Each card is a movie whose detail page carries a ``.seeds`` list of magnet
entries (``/link_start/?seed_id=…``); the magnet itself is base64-encoded in a
``const data = "…"`` script on the interstitial page, so detail resolution is
deliberately lazy.  Pan-only movies (no seed list) are skipped: this tool
searches magnets, not cloud-drive links.

The site's CDN blocks non-browser TLS fingerprints outright (no cookie is
issued to real browsers either).  When ``curl_cffi`` is installed we therefore
fetch pages with a Chrome-impersonating client; without it we fall back to
plain urllib and a 403/429 opens a process-local circuit breaker instead of
falling through mirrors or retrying the blocked request.

Optional user-supplied cookie replay: when ``.cache/seedhub-cookie.json``
exists (see ``tools/seedhub_cookie.py``), the browser session the user already
established with one mirror is replayed verbatim (Cookie + matching
User-Agent) and only that mirror is contacted.
"""

import base64
import copy
import datetime
import email.utils
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse

try:  # Chrome-TLS-impersonating client; optional, urllib is the fallback
    from curl_cffi import requests as _cffi_requests
except ImportError:  # pragma: no cover - depends on local install
    _cffi_requests = None

from .common import (
    BaseUrlRefresher,
    IndexerBlockedError,
    IndexerError,
    category_allowed,
    dedupe_results,
    dom,
    find_first,
    find_nodes,
    http_get,
    is_challenge,
    make_result,
    parse_size_bytes,
    text_of,
)
from .registry import INDEXERS


# HTTPS endpoints published by current GitHub configurations.  The final
# hostname comes from the open-source JS adapter.  The raw HTTP IP endpoint
# shown in some TVBox lists is intentionally excluded.
MIRRORS = (
    "https://sidhub.cc/",
    "https://seeduck.cc/",
    "https://hubdog.cc/",
    "https://www.seedhub.cc/",
)
_CONFIGURED_BASES = MIRRORS
BASE_URLS = BaseUrlRefresher("SeedHub", _CONFIGURED_BASES)


def configure_bases(bases=None):
    global _CONFIGURED_BASES
    _CONFIGURED_BASES = tuple(bases or MIRRORS)
    session = _load_cookie_session()
    BASE_URLS.update(((session[0],) if session else ()) + _CONFIGURED_BASES)


def configure_session(session=None, state_path=None):
    global _SESSION, _STATE_PATH
    _SESSION = session
    if state_path:
        _STATE_PATH = state_path


def clear_cache():
    with _CACHE_LOCK:
        _SEARCH_CACHE.clear()
        _DETAIL_CACHE.clear()


def refresh_base():
    """Touch a mirror so BaseUrlRefresher can record a working redirect origin."""
    if _load_cookie_session():
        base = _load_cookie_session()[0]
        _get_page(base)
        return
    errors = []
    for base in BASE_URLS.candidates(_CONFIGURED_BASES):
        try:
            _get_page(base)
            return
        except Exception as exc:
            BASE_URLS.failed(base)
            errors.append(type(exc).__name__)
    raise IndexerError("SeedHub mirrors failed: " + ",".join(errors[-3:]))


SEARCH_CACHE_SECONDS = 10 * 60
DETAIL_CACHE_SECONDS = 30 * 60
BLOCK_SECONDS = 30 * 60
MAX_RESULTS = 50
REQUEST_DELAY_MIN = float(INDEXERS["10"].get("request_delay_min", 0.1))
REQUEST_DELAY_MAX = float(INDEXERS["10"].get("request_delay_max", 0.5))

_CACHE_LOCK = threading.Lock()
_HTTP_LOCK = threading.Lock()
_SEARCH_CACHE = {}
_DETAIL_CACHE = {}
_BLOCKED_UNTIL = 0.0
_BLOCK_REASON = ""
_STATE_PATH = ""
_SESSION = None


def _load_cookie_session():
    """Only replay the browser session explicitly configured in NAStools."""
    return _SESSION


def _cache_get(cache, key, ttl):
    with _CACHE_LOCK:
        item = cache.get(key)
        if item and time.monotonic() - item[0] < ttl:
            return copy.deepcopy(item[1])
        if item:
            cache.pop(key, None)
    return None


def _cache_put(cache, key, value):
    with _CACHE_LOCK:
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[key] = (time.monotonic(), copy.deepcopy(value))


def _retry_after_seconds(error):
    value = error.headers.get("Retry-After", "") if error.headers else ""
    if value.isdigit():
        return max(60, min(int(value), 60 * 60))
    try:
        target = email.utils.parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=datetime.timezone.utc)
        seconds = int((target - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
        return max(60, min(seconds, 60 * 60))
    except (TypeError, ValueError, OverflowError):
        return BLOCK_SECONDS


def _open_circuit(reason, seconds=BLOCK_SECONDS):
    global _BLOCKED_UNTIL, _BLOCK_REASON
    with _CACHE_LOCK:
        _BLOCKED_UNTIL = max(_BLOCKED_UNTIL, time.monotonic() + seconds)
        _BLOCK_REASON = reason
    # Persist only the cooldown deadline/reason (never queries, links, cookies,
    # or response bodies), so repeatedly launching the short-lived CLI cannot
    # hammer a site that already rejected the previous process.
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        temporary = _STATE_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"blocked_until": time.time() + seconds, "reason": reason}, handle)
        os.replace(temporary, _STATE_PATH)
    except OSError:
        pass


def _persistent_block():
    try:
        with open(_STATE_PATH, encoding="utf-8") as handle:
            state = json.load(handle)
        return float(state.get("blocked_until", 0)) - time.time(), str(
            state.get("reason", "previous refusal")
        )
    except (OSError, ValueError, TypeError):
        return 0.0, ""


def _check_circuit():
    with _CACHE_LOCK:
        remaining = _BLOCKED_UNTIL - time.monotonic()
        reason = _BLOCK_REASON
    persisted_remaining, persisted_reason = _persistent_block()
    if persisted_remaining > remaining:
        remaining, reason = persisted_remaining, persisted_reason
    if remaining > 0:
        raise IndexerBlockedError(
            f"SeedHub request circuit is cooling down ({reason}, {remaining:.0f}s left)"
        )


def _trusted_detail_url(url):
    return BASE_URLS.trusts(url)


def _get_page(url):
    _check_circuit()
    session = _load_cookie_session()
    headers = {}
    if session and urllib.parse.urlparse(url).netloc == urllib.parse.urlparse(session[0]).netloc:
        # Replay the user's own browser session; cf_clearance is bound to the
        # exact User-Agent that solved the challenge, so it must match too.
        _base, user_agent, cookie = session
        headers = {"User-Agent": user_agent, "Cookie": cookie}
    try:
        page, final_url = _fetch(url, headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            seconds = _retry_after_seconds(exc) if exc.code == 429 else BLOCK_SECONDS
            _open_circuit(f"HTTP {exc.code}", seconds)
            raise IndexerBlockedError(
                f"SeedHub returned HTTP {exc.code}; stopped without bypass/retry"
            ) from exc
        raise
    if is_challenge(page):
        _open_circuit("browser challenge")
        raise IndexerBlockedError(
            "SeedHub browser challenge; stopped without bypass/retry"
        )
    return page, final_url


def _fetch(url, extra_headers):
    """Fetch one page under SeedHub's per-request rate limiter."""
    with _HTTP_LOCK:
        time.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))
        return _fetch_once(url, extra_headers)


def _fetch_once(url, extra_headers):
    """Fetch a page, preferring the Chrome-impersonating client.

    The SeedHub CDN rejects urllib's TLS fingerprint before any cookie or
    header matters, so curl_cffi (when available) is the primary path.  Any
    non-2xx status is normalised into urllib.error.HTTPError so callers see
    one error shape.
    """
    if _cffi_requests is None:
        raise IndexerError("SeedHub requires curl_cffi")
    return http_get(url, timeout=20, headers=extra_headers, client=_cffi_requests)


def parse_search_page(text, base, requested_categories=None):
    """Parse the ``.cover`` card layout used by public SeedHub adapters."""
    root = dom(text)
    results = []
    result_categories = [2000, 5000, 8000]
    if not category_allowed(result_categories, requested_categories):
        return []
    for cover in find_nodes(root, cls="cover"):
        link = find_first(cover, "a", predicate=lambda n: bool(n.attrs.get("href")))
        image = find_first(cover, "img")
        if link is None:
            continue
        href = urllib.parse.urljoin(base, link.attrs.get("href", ""))
        if not _trusted_detail_url(href):
            continue
        title = (image.attrs.get("alt", "") if image else "") or text_of(link)
        if not title.strip():
            continue
        card_text = text_of(cover.parent or cover)
        result = make_result(
            10,
            title,
            size=parse_size_bytes(card_text),
            seeders=0,
            leechers=0,
            details=href,
            categories=result_categories,
        )
        result["detailsUrl"] = href
        result["metadataIncomplete"] = not bool(result["size"])
        results.append(result)
    return dedupe_results(results)


_MAGNET_RE = re.compile(r"magnet:\?[^\s\"'<>]+", re.I)


def parse_detail_page(text):
    """Return the movie's magnet seed entries from its detail page.

    Each entry is ``{"title", "size", "url"}`` where url is the movie page's
    ``/link_start/?seed_id=…`` interstitial (the magnet is base64 inside it)
    or a literal magnet URI for rare inline links.  Pan-only movies have no
    ``.seeds`` list and yield no entries.
    """
    root = dom(text)
    entries = []
    for container in find_nodes(root, cls="seeds"):
        for li in find_nodes(container, tag="li"):
            link = find_first(li, "a", predicate=lambda n: bool(n.attrs.get("href")))
            if link is None:
                continue
            href = (link.attrs.get("href") or "").strip()
            if not (href.startswith("/link_start/") or _MAGNET_RE.match(href)):
                continue
            title = (link.attrs.get("title") or text_of(link) or "").strip()
            size_node = find_first(li, cls="size")
            size_text = text_of(size_node) if size_node else ""
            # site writes "72.71G" as often as "10.49GB"; normalise for the parser
            size_text = re.sub(
                r"^([0-9.]+)\s*([KMGT])$", r"\1\2B", size_text.strip())
            entries.append({
                "title": title,
                "size": parse_size_bytes(size_text),
                "url": href,
            })
    # Rare inline magnet links outside the .seeds list
    for value in _MAGNET_RE.findall(text):
        entries.append({"title": "", "size": 0, "url": value})
    return entries


_BASE64_MAGNET_RE = re.compile(
    r'const\s+data\s*=\s*"([A-Za-z0-9+/=]+)"'
)


def parse_seed_page(text):
    """Decode the magnet from a ``/link_start/?seed_id=`` interstitial page."""
    match = _BASE64_MAGNET_RE.search(text)
    if not match:
        return ""
    try:
        decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    return decoded if decoded.lower().startswith("magnet:") else ""


def _round_robin_results(groups):
    """Interleave releases from movie cards so one card cannot fill the cap."""
    output = []
    for position in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if position < len(group):
                output.append(group[position])
    return output


def search(query, categories=None, fetch=150):
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(fetch or MAX_RESULTS), MAX_RESULTS))
    cache_key = (query.casefold(), tuple(sorted(str(x) for x in (categories or []))))
    cached = _cache_get(_SEARCH_CACHE, cache_key, SEARCH_CACHE_SECONDS)
    if cached is not None:
        return cached[:limit]

    errors = []
    session = _load_cookie_session()
    # With a user-supplied browser session, cookies are domain-bound: try only
    # the mirror the session belongs to instead of rotating through MIRRORS.
    if session:
        BASE_URLS.update((session[0],) + tuple(_CONFIGURED_BASES))
        bases = (session[0],)
    else:
        bases = BASE_URLS.candidates(_CONFIGURED_BASES)
    path = "s/" + urllib.parse.quote(query, safe="") + "/?page=1"
    for base in bases:
        try:
            page, final_url = _get_page(urllib.parse.urljoin(base, path))
            if "cover" not in page:
                raise IndexerBlockedError(
                    "unrecognised SeedHub search page; stopped without retry"
                )
            BASE_URLS.observe(base, final_url)
            cards = parse_search_page(page, final_url, categories)
            # Expand each movie card into its individual magnet seed entries
            # so quality/size ranking can operate on actual releases.
            result_groups = []
            for card in cards[:_MAX_CARDS]:
                try:
                    detail_page, detail_url = _get_page(card["detailsUrl"])
                except IndexerBlockedError:
                    raise
                except Exception:
                    continue  # one dead detail page must not kill the search
                BASE_URLS.observe(card["detailsUrl"], detail_url)
                card_results = []
                for entry in parse_detail_page(detail_page):
                    if entry["url"].startswith("magnet:"):
                        magnet, link = entry["url"], ""
                    else:
                        magnet, link = "", urllib.parse.urljoin(detail_url, entry["url"])
                    result = make_result(
                        10,
                        entry["title"] or card["title"],
                        size=entry["size"] or card["size"],
                        seeders=0,
                        leechers=0,
                        details=card["detailsUrl"],
                        magnet=magnet,
                        download=link,
                        categories=card["categories"],
                    )
                    if link:
                        result["detailsUrl"] = link
                        result["metadataIncomplete"] = not bool(result["size"])
                    card_results.append(result)
                if card_results:
                    result_groups.append(card_results)
            results = _round_robin_results(result_groups)
            _cache_put(_SEARCH_CACHE, cache_key, results)
            return results[:limit]
        except IndexerBlockedError:
            raise
        except Exception as exc:
            BASE_URLS.failed(base)
            errors.append(type(exc).__name__)
    raise IndexerError("SeedHub HTTPS mirrors failed: " + "; ".join(errors[-3:]))


# How many movie cards get their detail page fetched per search.  Detail pages
# are the expensive part (one request each); the rest of the pipeline only
# needs a handful of candidates for quality ranking.
_MAX_CARDS = 5


def resolve_result(result):
    """Resolve one seed entry to its magnet URI.

    Entries found by ``search`` carry either the magnet directly (inline) or a
    ``/link_start/?seed_id=`` URL whose page base64-encodes the magnet.
    """
    magnet = (result.get("magnetUrl") or "").strip()
    if magnet.startswith("magnet:"):
        return magnet
    url = (result.get("detailsUrl") or "").strip()
    if not url or not _trusted_detail_url(url):
        return ""
    cached = _cache_get(_DETAIL_CACHE, url, DETAIL_CACHE_SECONDS)
    if cached is not None:
        return cached
    page, _final_url = _get_page(url)
    resolved = parse_seed_page(page)
    _cache_put(_DETAIL_CACHE, url, resolved)
    return resolved

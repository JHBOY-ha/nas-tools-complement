"""52BT adapter, including its rotating-address discovery algorithm."""

import datetime
import re
import time
import urllib.parse

from .common import (
    BaseUrlRefresher,
    IndexerError,
    category_allowed,
    dedupe_results,
    dom,
    find_first,
    find_nodes,
    http_get,
    is_challenge,
    int_of,
    make_result,
    parse_date,
    parse_size_bytes,
    text_of,
)


PUBLISHER = "https://www.52btbt.icu/"
DEFAULT_CONFIG = {
    "domains": ["529075.xyz", "529076.xyz"],
    "interval": 30,
    "length": 8,
    "salt": "address-page-2026",
}
_CACHE = {"expires": 0.0, "bases": []}
_STATIC_FALLBACKS = (
    "https://2esrozvx.529075.xyz/",
    "https://q6nvk9f5.529076.xyz/",
)
BASE_URLS = BaseUrlRefresher("52BT", _STATIC_FALLBACKS)
_OVERRIDE_BASES = ()
_CATEGORY_PREFIX_RE = re.compile(
    r"^\s*(?:【|\[)\s*(?:video|music|document|other)\s*(?:】|\])\s*",
    re.I,
)


def configure_bases(bases=None):
    global _OVERRIDE_BASES
    had_override = bool(_OVERRIDE_BASES)
    _OVERRIDE_BASES = tuple(bases or ())
    if _OVERRIDE_BASES:
        BASE_URLS.update(_OVERRIDE_BASES)
    elif had_override:
        BASE_URLS.update(_STATIC_FALLBACKS)


def _js_hash32(text):
    value = 2166136261
    for char in text:
        value ^= ord(char)
        value = (value * 16777619) & 0xffffffff
    return value


def _address_code(seed_text, length):
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    state = _js_hash32(seed_text) or 1
    output = []
    for _ in range(length):
        state ^= (state << 13) & 0xffffffff
        state ^= state >> 17
        state ^= (state << 5) & 0xffffffff
        state &= 0xffffffff
        output.append(alphabet[state % len(alphabet)])
    return "".join(output)


def discover_bases():
    now = time.time()
    if _CACHE["bases"] and now < _CACHE["expires"]:
        return list(_CACHE["bases"])

    config = dict(DEFAULT_CONFIG)
    try:
        page, _ = http_get(PUBLISHER, timeout=20)
        domains = re.search(r"domains\s*:\s*\[([^]]+)\]", page)
        interval = re.search(r"intervalMinutes\s*:\s*(\d+)", page)
        length = re.search(r"codeLength\s*:\s*(\d+)", page)
        salt = re.search(r"salt\s*:\s*[\"']([^\"']+)", page)
        if domains:
            parsed = re.findall(r"[\"']([^\"']+)[\"']", domains.group(1))
            if parsed:
                config["domains"] = parsed
        if interval and 1 <= int(interval.group(1)) <= 1440:
            config["interval"] = int(interval.group(1))
        if length and 1 <= int(length.group(1)) <= 32:
            config["length"] = int(length.group(1))
        if salt:
            config["salt"] = salt.group(1)
    except Exception:
        pass

    slot = int(now // (config["interval"] * 60))
    bases = []
    for candidate_slot in (slot, slot - 1, slot + 1):
        for index, domain in enumerate(config["domains"][:8]):
            host = domain.strip().lower().removeprefix("https://").rstrip("/")
            seed = f"{config['salt']}|{host}|{candidate_slot}|{index}"
            bases.append(
                f"https://{_address_code(seed, config['length'])}.{host}/")
    bases.extend(_STATIC_FALLBACKS)
    _CACHE.update(
        expires=now + 20 * 60,
        bases=list(dict.fromkeys(bases)),
    )
    return list(_CACHE["bases"])


def _category_id(categories):
    wanted = {int_of(category, -1) for category in (categories or [])}
    if 3000 in wanted:
        return 2
    if 7000 in wanted:
        return 4
    if 8000 in wanted:
        return 7
    return 1


def _categories(label):
    label = (label or "").lower()
    if "video" in label:
        return [2000, 5000]
    if "music" in label:
        return [3000]
    if "document" in label:
        return [7000]
    return [8000]


def parse_page(text, base, requested_categories=None):
    root = dom(text)
    results = []
    for article in find_nodes(root, "article", cls="resource-card"):
        link = find_first(
            article,
            "a",
            predicate=lambda node: node.attrs.get("href", "").startswith("/hash/"),
        )
        if link is None:
            continue
        href = link.attrs.get("href", "")
        hash_match = re.search(r"([a-fA-F0-9]{40})", href)
        if not hash_match:
            continue
        metadata = [text_of(span) for span in find_nodes(article, "span")]
        category_label = next(
            (
                re.sub(r"^Type\s*[：:]\s*", "", item, flags=re.I)
                for item in metadata
                if re.match(r"^Type\s*[：:]", item, re.I)
            ),
            "Other",
        )
        result_categories = _categories(category_label)
        if not category_allowed(result_categories, requested_categories):
            continue
        date_text = next(
            (
                re.sub(r"^Added\s*[：:]\s*", "", item, flags=re.I)
                for item in metadata
                if re.match(r"^Added\s*[：:]", item, re.I)
            ),
            "",
        )
        size_text = next(
            (
                re.sub(r"^Size\s*[：:]\s*", "", item, flags=re.I)
                for item in metadata
                if re.match(r"^Size\s*[：:]", item, re.I)
            ),
            "",
        )
        published = parse_date(
            date_text,
            ["%Y-%m-%d", "%Y-%m-%d %H:%M:%S"],
            datetime.timezone(datetime.timedelta(hours=8)),
        )
        title = _CATEGORY_PREFIX_RE.sub("", text_of(link)).strip()
        results.append(make_result(
            1,
            title,
            size=parse_size_bytes(size_text),
            seeders=1,
            leechers=1,
            details=urllib.parse.urljoin(base, href),
            infohash=hash_match.group(1),
            categories=result_categories,
            published=published,
        ))
    return results


def search(query, categories=None, fetch=150):
    keyword = urllib.parse.quote(query, safe="")
    category_id = _category_id(categories)
    errors = []
    for base in BASE_URLS.candidates(_OVERRIDE_BASES or discover_bases()):
        results = []
        try:
            for page_number in (1, 2):
                path = (
                    f"search-{keyword}-{category_id}-2-{page_number}.html?lang=en"
                )
                page, final_url = http_get(urllib.parse.urljoin(base, path))
                if (
                    is_challenge(page)
                    or "地址已经失效" in page
                    or "该地址已经失效" in page
                    or "Internal Server Error" in page
                ):
                    raise IndexerError("expired or challenged mirror")
                if "resource-card" not in page and "search" not in page.lower():
                    raise IndexerError("unrecognised search page")
                results.extend(parse_page(page, final_url, categories))
                if len(results) >= fetch:
                    break
            BASE_URLS.observe(base, final_url)
            return dedupe_results(results)[:fetch]
        except Exception as exc:
            BASE_URLS.failed(base)
            errors.append(type(exc).__name__)
    raise IndexerError("52BT mirrors failed: " + "; ".join(errors[-3:]))

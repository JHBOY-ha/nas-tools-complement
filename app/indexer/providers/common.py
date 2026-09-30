"""Standard-library HTTP, HTML and result-normalisation helpers."""

import datetime
import html.parser
import ipaddress
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from contextlib import contextmanager
from contextvars import ContextVar

import requests

from .registry import INDEXERS


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_NETWORK = ContextVar("public_indexer_network", default={})


@contextmanager
def network_options(proxies=None, headers=None, seconds=90):
    token = _NETWORK.set({"proxies": proxies or {}, "headers": headers or {},
                          "deadline": time.monotonic() + seconds})
    try:
        yield
    finally:
        _NETWORK.reset(token)


class IndexerError(RuntimeError):
    """A built-in indexer could not be reached or parsed."""


class IndexerBlockedError(IndexerError):
    """An indexer explicitly refused automation; callers must not retry."""


def _https_origin(value):
    """Return a canonical HTTPS origin suitable for joining relative links."""
    try:
        parsed = urllib.parse.urlparse(value or "")
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    if parsed.username or parsed.password or any(c.isspace() for c in value):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    authority = host if port in (None, 443) else f"{host}:{port}"
    return f"https://{authority}/"


def validate_public_url(url):
    """Validate each request, including redirects, before contacting a public site."""
    origin = _https_origin(url)
    if not origin:
        raise IndexerError("Invalid HTTPS address")
    parsed = urllib.parse.urlparse(origin)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443,
                                       type=socket.SOCK_STREAM)
    except OSError as exc:
        raise IndexerError("DNS lookup failed") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global
                            for item in addresses):
        raise IndexerError("Public indexers cannot access local/private addresses")


class BaseUrlRefresher:
    """Keep a validated indexer base fresh without trusting arbitrary URLs.

    Adapters start from configured HTTPS entry points.  After a structurally
    valid page is fetched, its final redirect origin is promoted and cached.
    Only an origin reached from a configured or previously learned origin can
    be learned, and callers must call ``observe`` only after validating the
    page.  A failed learned origin is discarded so the next configured mirror
    can refresh it again.
    """

    def __init__(self, name, bases=(), *, ttl_seconds=60 * 60):
        self.name = name
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.Lock()
        self._configured = ()
        self._learned = {}
        self._preferred = ""
        self._refreshed_at = None
        self.update(bases)

    def update(self, bases):
        normalized = tuple(dict.fromkeys(
            origin for origin in (_https_origin(base) for base in bases) if origin
        ))
        with self._lock:
            if normalized != self._configured:
                self._learned = {}
                self._preferred = ""
                self._refreshed_at = None
            self._configured = normalized

    def reset(self):
        with self._lock:
            self._learned.clear()
            self._preferred = ""
            self._refreshed_at = None

    def _prune(self, now):
        self._learned = {
            origin: expires_at for origin, expires_at in self._learned.items()
            if expires_at > now
        }
        if self._preferred and self._preferred not in self._learned:
            self._preferred = ""

    def candidates(self, bases=None):
        if bases is not None:
            self.update(bases)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return list(dict.fromkeys(
                ([self._preferred] if self._preferred else [])
                + list(self._configured)
                + list(self._learned)
            ))

    def observe(self, requested_url, final_url):
        requested = _https_origin(requested_url)
        final = _https_origin(final_url)
        if not requested or not final:
            return ""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            trusted = set(self._configured) | set(self._learned)
            if requested not in trusted:
                return ""
            self._learned[final] = now + self.ttl_seconds
            self._preferred = final
            self._refreshed_at = time.time()
        return final

    def failed(self, value):
        origin = _https_origin(value)
        if not origin:
            return
        with self._lock:
            if origin == self._preferred:
                self._preferred = ""
            if origin not in self._configured:
                self._learned.pop(origin, None)

    def trusts(self, value):
        origin = _https_origin(value)
        if not origin:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return origin in set(self._configured) | set(self._learned)

    def status(self):
        candidates = self.candidates()
        with self._lock:
            preferred = self._preferred
            refreshed_at = self._refreshed_at
        return {
            "base_url": preferred or (candidates[0] if candidates else None),
            "base_source": "refreshed" if preferred else "configured",
            "base_refreshed_at": refreshed_at,
            "configured_bases": list(self._configured),
        }


def http_get(url, *, encoding=None, timeout=20, headers=None, client=None):
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    options = _NETWORK.get()
    request_headers.update(options.get("headers", {}))
    request_headers.update(headers or {})
    session = (client or requests).Session()
    session.trust_env = False
    try:
        for _ in range(6):
            remaining = options.get("deadline", time.monotonic() + timeout) - time.monotonic()
            if remaining <= 0:
                raise IndexerError("Indexer request budget exceeded")
            validate_public_url(url)
            kwargs = {"impersonate": "chrome"} if client else {}
            response = session.get(url, headers=request_headers, timeout=min(timeout, remaining),
                                   proxies=options.get("proxies", {}), verify=True,
                                   allow_redirects=False, stream=True, **kwargs)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urllib.parse.urljoin(url, response.headers.get("Location", ""))
                    if _https_origin(target) != _https_origin(url):
                        request_headers.pop("Cookie", None)
                    # Do not let a redirect carry a browser session to another mirror.
                    session.cookies.clear()
                    url = target
                    continue
                if not 200 <= response.status_code < 300:
                    raise urllib.error.HTTPError(url, response.status_code, "Indexer HTTP error",
                                                 response.headers, None)
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise IndexerError("Indexer page exceeds 4 MiB")
                    chunks.append(chunk)
                charset = encoding or "utf-8"
                return b"".join(chunks).decode(charset, errors="replace"), url
            finally:
                response.close()
        raise IndexerError("Too many indexer redirects")
    finally:
        session.close()


def http_get_bytes(url, *, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class HtmlNode:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.parent = parent
        self.children = []


class _DomParser(html.parser.HTMLParser):
    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("_root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(tag.lower(), attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if node.tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(
            HtmlNode(tag.lower(), attrs, self.stack[-1]))

    def handle_endtag(self, tag):
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def dom(text):
    parser = _DomParser()
    parser.feed(text)
    return parser.root


def walk(node):
    for child in node.children:
        if isinstance(child, HtmlNode):
            yield child
            yield from walk(child)


def classes(node):
    return set(node.attrs.get("class", "").split())


def find_nodes(node, tag=None, *, cls=None, predicate=None):
    found = []
    for item in walk(node):
        if tag and item.tag != tag:
            continue
        if cls and cls not in classes(item):
            continue
        if predicate and not predicate(item):
            continue
        found.append(item)
    return found


def find_first(node, tag=None, *, cls=None, predicate=None):
    items = find_nodes(node, tag, cls=cls, predicate=predicate)
    return items[0] if items else None


def text_of(node, *, skip_tags=None):
    if node is None:
        return ""
    skip_tags = set(skip_tags or ())
    chunks = []

    def collect(item):
        for child in item.children:
            if isinstance(child, str):
                chunks.append(child)
            elif child.tag not in skip_tags:
                collect(child)

    collect(node)
    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def direct_children(node, tag):
    return [child for child in node.children
            if isinstance(child, HtmlNode) and child.tag == tag]


def parse_size_bytes(value):
    match = re.search(
        r"([0-9]+(?:[.,][0-9]+)?)\s*(B|K(?:I)?B|M(?:I)?B|G(?:I)?B|T(?:I)?B|P(?:I)?B)\b",
        value or "", re.I)
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper().replace("IB", "B")
    power = ("B", "KB", "MB", "GB", "TB", "PB").index(unit)
    return int(number * (1024 ** power))


def int_of(value, default=0):
    match = re.search(r"-?\d+", str(value or ""))
    return int(match.group()) if match else default


def parse_date(value, formats, tz):
    for fmt in formats:
        try:
            return datetime.datetime.strptime(value.strip(), fmt).replace(tzinfo=tz)
        except (TypeError, ValueError):
            pass
    return None


def category_allowed(result_categories, requested):
    if not requested:
        return True
    wanted = {int_of(category, -1) for category in requested}
    return bool(set(result_categories) & wanted)


def make_result(indexer_id, title, *, size=0, seeders=0, leechers=0,
                details="", download="", magnet="", infohash="",
                categories=None, published=None, files=None):
    indexer = INDEXERS[str(indexer_id)]
    infohash = (infohash or "").strip()
    if not magnet and infohash:
        magnet = (f"magnet:?xt=urn:btih:{infohash}&dn="
                  f"{urllib.parse.quote(title or '')}")
    age_hours = None
    if published is not None:
        if published.tzinfo is None:
            published = published.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        age_hours = max(
            0.0,
            (now - published.astimezone(datetime.timezone.utc)).total_seconds()
            / 3600,
        )
    result = {
        "title": (title or "").strip(),
        "indexer": indexer["name"],
        "indexerId": indexer["id"],
        "guid": details or magnet or download,
        "downloadUrl": download or magnet,
        "magnetUrl": magnet,
        "infoHash": infohash,
        "size": int(size or 0),
        "seeders": seeders,
        "leechers": leechers,
        "categories": list(categories or []),
        "ageHours": age_hours,
    }
    if files is not None:
        result["files"] = files
    return result


def dedupe_results(results):
    output = []
    seen = set()
    for result in results:
        key = (result.get("infoHash") or result.get("guid")
               or (result.get("title"), result.get("size")))
        if key in seen:
            continue
        seen.add(key)
        output.append(result)
    return output


def is_challenge(text):
    lower = (text or "").lower()
    return ("just a moment" in lower and "cloudflare" in lower
            or "cf-chl-" in lower
            or "enable javascript and cookies to continue" in lower)

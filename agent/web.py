"""Web access for Agent Jo: search and page fetching, stdlib only.

Search uses DuckDuckGo's HTML endpoints (no API key). Fetching extracts the
readable text of a page. Both go through a guard that refuses private,
loopback, and link-local addresses (including on redirects) unless
AGENT_ALLOW_LOCAL_FETCH is set — so a prompt-injected instruction can't point
the agent at internal services.

Privacy: everything in this module makes outbound requests. The UI exposes a
Web access toggle (tools.WEB_ENABLED) checked by the tool handlers, not here.
"""

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import config

SEARCH_URL_HTML = "https://html.duckduckgo.com/html/?q="
SEARCH_URL_LITE = "https://lite.duckduckgo.com/lite/?q="
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAX_BYTES = 1_500_000


class WebError(Exception):
    """A web operation failed, with a user-presentable message."""


# ---------------------------------------------------------------------- #
# Address guard (applies to the first request AND every redirect hop)
# ---------------------------------------------------------------------- #
def _is_private_host(host: str) -> bool:
    """True if the host is/resolves to a private, loopback, link-local, or
    reserved address."""
    host = (host or "").strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)
    except ValueError:
        pass                                   # a hostname: resolve it
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise WebError(f"Could not resolve host '{host}'.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return True
    return False


def _check_url_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebError(f"Only http/https URLs are allowed, got '{parsed.scheme}'.")
    if not parsed.hostname:
        raise WebError("URL has no host.")
    if not config.ALLOW_LOCAL_FETCH and _is_private_host(parsed.hostname):
        raise WebError(
            f"Refusing to fetch private/internal address '{parsed.hostname}' "
            "(set AGENT_ALLOW_LOCAL_FETCH=1 to permit local fetches).")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GuardedRedirect)


def _get(url: str, timeout: int = 20) -> tuple[str, str]:
    """GET a URL through the guard. Returns (text, content_type)."""
    _check_url_allowed(url)
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept-Language": "en"})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_BYTES)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise WebError(f"HTTP {exc.code} fetching {url}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise WebError(f"Could not reach {url}: {exc}")
    return raw.decode(charset, errors="replace"), ctype


# ---------------------------------------------------------------------- #
# Search (DuckDuckGo html endpoint, lite as fallback)
# ---------------------------------------------------------------------- #
def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = " ".join(html.unescape(text).split())
    return re.sub(r"\s+([.,;:!?])", r"\1", text)   # no space before punctuation


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', attrs or "", re.I)
    return m.group(1) if m else ""


def _resolve_ddg_url(href: str) -> str:
    """DDG result links are often redirect wrappers carrying the real URL in
    a 'uddg' parameter — unwrap them."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in (parsed.netloc or "") and parsed.path.startswith("/l"):
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("uddg"):
            return urllib.parse.unquote(qs["uddg"][0])
        return ""
    if href.startswith("/"):
        return ""                              # site-relative DDG link: skip
    return href


_A_TAG = re.compile(r"<a\s([^>]*)>(.*?)</a>", re.I | re.S)


def _parse_html_results(page: str, max_results: int) -> list[dict]:
    out = []
    anchors = list(_A_TAG.finditer(page))
    for i, m in enumerate(anchors):
        attrs, inner = m.group(1), m.group(2)
        if "result__a" not in attrs:
            continue
        url = _resolve_ddg_url(_attr(attrs, "href"))
        title = _clean(inner)
        if not url or not title:
            continue
        end = len(page)
        for m2 in anchors[i + 1:]:
            if "result__a" in m2.group(1):
                end = m2.start()
                break
        snip = re.search(r'result__snippet[^>]*>(.*?)</(?:a|div|td|span)>',
                         page[m.end():end], re.I | re.S)
        out.append({"title": title, "url": url,
                    "snippet": _clean(snip.group(1)) if snip else ""})
        if len(out) >= max_results:
            break
    return out


def _parse_lite_results(page: str, max_results: int) -> list[dict]:
    out = []
    snippets = [_clean(x) for x in re.findall(
        r'class="result-snippet"[^>]*>(.*?)</td>', page, re.I | re.S)]
    idx = 0
    for m in _A_TAG.finditer(page):
        attrs, inner = m.group(1), m.group(2)
        if "result-link" not in attrs:
            continue
        url = _resolve_ddg_url(_attr(attrs, "href"))
        title = _clean(inner)
        if not url or not title:
            continue
        out.append({"title": title, "url": url,
                    "snippet": snippets[idx] if idx < len(snippets) else ""})
        idx += 1
        if len(out) >= max_results:
            break
    return out


def search(query: str, max_results: int | None = None) -> list[dict]:
    """Search the web. Returns [{title, url, snippet}, ...]. Raises WebError
    if both endpoints fail."""
    query = (query or "").strip()
    if not query:
        return []
    k = max(1, min(int(max_results or config.WEB_SEARCH_RESULTS), 8))
    q = urllib.parse.quote_plus(query)
    first_err = None
    try:
        page, _ = _get(SEARCH_URL_HTML + q)
        results = _parse_html_results(page, k)
        if results:
            return results
    except WebError as exc:
        first_err = exc
    try:
        page, _ = _get(SEARCH_URL_LITE + q)
        return _parse_lite_results(page, k)
    except WebError as exc:
        raise WebError(f"Search failed ({first_err or exc}).")


# ---------------------------------------------------------------------- #
# Page fetching -> readable text
# ---------------------------------------------------------------------- #
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "header", "footer", "table", "ul", "ol",
             "blockquote", "pre"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _extract_text(page_html: str) -> str:
    ex = _TextExtractor()
    try:
        ex.feed(page_html)
    except Exception:
        pass
    text = "".join(ex.parts)
    text = re.sub(r"[ \t\r]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    _extract_text.last_title = " ".join(ex.title.split())  # type: ignore
    return text.strip()


def _diagnose(raw_html: str, text: str) -> str:
    """When a fetch comes back thin, guess why so the agent can change tactics
    instead of blindly retrying."""
    low = (raw_html or "").lower()
    markers = [
        ("cloudflare / anti-bot challenge", ("just a moment", "cf-browser-verification",
                                             "challenge-platform", "cf-chl", "checking your browser")),
        ("CAPTCHA wall", ("g-recaptcha", "hcaptcha", "captcha")),
        ("login / auth wall", ("please log in", "sign in to continue", "authentication required")),
        ("access blocked / rate limited", ("access denied", "403 forbidden",
                                           "too many requests", "rate limit")),
        ("JavaScript-rendered app (content not in HTML)", ("__next_data__", "window.__nuxt__",
                                                           "id=\"root\"></div>", "id=\"app\"></div>",
                                                           "please enable javascript")),
    ]
    hits = [label for label, keys in markers if any(k in low for k in keys)]
    if not hits and len(text) < 200 and len(raw_html) > 2000:
        hits = ["content likely rendered client-side or hidden behind a script"]
    if not hits:
        return ""
    return ("Likely cause: " + "; ".join(hits)
            + ". Try: screenshot the page, use the site's own search/API, or a "
              "JS-capable fetch (Playwright) / different selector — don't just retry the same call.")


def fetch(url: str, max_chars: int | None = None) -> dict:
    """Fetch a page and return {'title', 'url', 'text', 'truncated', 'diagnostics'}."""
    max_chars = max_chars or config.WEB_FETCH_MAX_CHARS
    body, ctype = _get(url)
    if ctype and not any(t in ctype for t in ("text/", "html", "xml", "json")):
        raise WebError(f"Not a readable text page (content-type: {ctype}).")
    text = _extract_text(body)
    title = getattr(_extract_text, "last_title", "") or url
    truncated = len(text) > max_chars
    diag = _diagnose(body, text) if len(text) < 400 else ""
    return {"title": title, "url": url, "text": text[:max_chars],
            "truncated": truncated, "diagnostics": diag}

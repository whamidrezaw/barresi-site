"""Turn fetched HTML / feeds into normalised, comparable content.

The same Python extractor is used for both engines (raw HTTP HTML and the
browser-rendered DOM), so switching engines never changes how text is read.
Hidden elements (accordions, tabs) are included on purpose: embassy pages often
keep important details in collapsed sections.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import lxml.html
from lxml import etree
from lxml.cssselect import CSSSelector

from .util import clean_text, has_alnum, sha256_text, truncate

SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "canvas", "head", "title",
    "meta", "link", "iframe", "object", "embed", "audio", "video", "picture", "source",
    "track", "map",
})
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
    "dialog", "dir", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "html", "legend", "li", "main",
    "menu", "nav", "ol", "optgroup", "option", "p", "section", "summary", "table", "tbody",
    "tfoot", "thead", "tr", "ul",
})
CELL_TAGS = frozenset({"td", "th"})
# Cookie/consent overlays injected by well-known consent managers.
DEFAULT_EXCLUDES = (
    "#onetrust-consent-sdk", "#CybotCookiebotDialog", "#usercentrics-root", ".cc-window",
    "#cookie-law-info-bar", "#cmplz-cookiebanner-container",
)
# Used for crawled sub-pages when the configured selector is missing there.
FALLBACK_SELECTORS = ("main", "[role=main]", "article", "#content", "#main", ".content")
FILE_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".ods", ".zip",
})
ASSET_EXTENSIONS = frozenset({
    ".css", ".js", ".mjs", ".json", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".bmp", ".tif", ".tiff", ".avif", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".webm", ".ogg", ".wav", ".avi", ".mov", ".mkv", ".m4a", ".exe", ".msi", ".dmg", ".apk",
    ".rar", ".7z", ".gz", ".tar", ".xml", ".rss", ".atom",
})
MAX_LINES = 20000
MAX_DEPTH = 400
_CELL = ""
_TRACKING_PARAM = re.compile(
    r"^(utm_\w+|fbclid|gclid|dclid|gbraid|wbraid|msclkid|mc_cid|mc_eid|yclid|igshid|_ga|_gl|ref_src)$", re.I
)
_PCT_RE = re.compile(r"%[0-9a-fA-F]{2}")
_EMPTY_CELLS = re.compile(r"(?:\s*\|\s*)+")
_UNSAFE_PATH_RE = re.compile(r"[^\x21-\x7e]|[\"<>\\^`{|}]")


class ExtractError(Exception):
    """The fetched document does not contain usable content."""

    def __init__(self, message: str, kind: str = "content"):
        super().__init__(message)
        self.kind = kind  # selector | short | type | parse | empty


@dataclass
class Snapshot:
    lines: list
    links: list = field(default_factory=list)  # [(text, url)]
    title: str = ""

    @property
    def digest(self) -> str:
        return sha256_text(json.dumps([self.lines, self.links], ensure_ascii=False))

    @property
    def text_length(self) -> int:
        return sum(len(line) for line in self.lines)

    def to_json(self) -> str:
        return json.dumps({"v": 1, "title": self.title, "lines": self.lines, "links": self.links}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Snapshot:
        data = json.loads(raw)
        return cls(list(data.get("lines", [])), [tuple(x) for x in data.get("links", [])], data.get("title", ""))


@dataclass
class Item:
    id: str
    title: str
    url: str | None
    text: str
    published: str = ""


# --------------------------------------------------------------------------- URLs

def normalize_url(url: str) -> str | None:
    """Canonical absolute http(s) URL without fragment or tracking parameters."""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        netloc = parts.netloc.lower()
        if scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[:-4]
        path = parts.path or "/"
        path = _UNSAFE_PATH_RE.sub(lambda m: quote(m.group()), path)
        path = _PCT_RE.sub(lambda m: m.group().upper(), path)
        query = parts.query
        if query:
            pairs = parse_qsl(query, keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if not _TRACKING_PARAM.match(k)]
            if len(kept) != len(pairs):
                query = urlencode(kept)
        return urlunsplit((scheme, netloc, path, query, ""))
    except ValueError:
        return None


def url_extension(url: str) -> str:
    path = urlsplit(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    return name[name.rfind("."):] if "." in name else ""


def same_site(host_a: str | None, host_b: str | None) -> bool:
    strip = lambda h: (h or "").lower().removeprefix("www.")
    return strip(host_a) == strip(host_b)


# --------------------------------------------------------------------------- HTML

def parse_html(text: str, url: str) -> lxml.html.HtmlElement:
    if not text or not text.strip():
        raise ExtractError("empty document", kind="empty")
    parser = lxml.html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)
    try:
        doc = lxml.html.document_fromstring(text.encode("utf-8", "replace"), parser=parser, base_url=url)
    except (etree.ParserError, ValueError) as exc:
        raise ExtractError(f"could not parse HTML: {exc}", kind="parse") from None
    return doc


def document_base(doc, url: str) -> str:
    base = doc.find(".//base[@href]")
    if base is not None:
        try:
            return urljoin(url, base.get("href", "").strip())
        except ValueError:
            pass
    return url


def document_title(doc) -> str:
    return clean_text(doc.findtext(".//title") or "")


@lru_cache(maxsize=512)
def _compiled(selector: str) -> CSSSelector:
    return CSSSelector(selector, translator="html")


def select_all(doc, selector: str) -> list:
    """All matches of a CSS selector, without matches nested inside other matches."""
    found = _compiled(selector)(doc)
    if len(found) < 2:
        return found
    matched = set(found)
    return [el for el in found if not any(a in matched for a in el.iterancestors())]


def drop_selectors(doc, selectors) -> None:
    for selector in selectors:
        for el in _compiled(selector)(doc):
            if el.getparent() is not None:
                el.drop_tree()


class _Lines:
    __slots__ = ("lines", "_buf", "_limit")

    def __init__(self, limit: int):
        self.lines: list[str] = []
        self._buf: list[str] = []
        self._limit = limit

    def text(self, value: str | None) -> None:
        if value:
            self._buf.append(value)

    def cell(self) -> None:
        self._buf.append(_CELL)

    def br(self) -> None:
        if not self._buf:
            return
        raw = "".join(self._buf)
        self._buf.clear()
        if _CELL in raw:
            line = " | ".join(p for p in (clean_text(part) for part in raw.split(_CELL)) if p)
        else:
            line = clean_text(raw)
        if line and has_alnum(line) and len(self.lines) < self._limit:
            self.lines.append(line)


def _walk(el, out: _Lines, depth: int) -> None:
    tag = el.tag
    if not isinstance(tag, str):  # comment / processing instruction
        return
    tag = tag.lower()
    if tag in SKIP_TAGS or depth > MAX_DEPTH:
        return
    if tag == "br":
        out.br()
        return
    if tag == "pre":
        out.br()
        for line in el.text_content().splitlines():
            out.text(line)
            out.br()
        return
    block = tag in BLOCK_TAGS
    if block:
        out.br()
    out.text(el.text)
    for child in el:
        _walk(child, out, depth + 1)
        out.text(child.tail)
    if tag in CELL_TAGS:
        out.cell()
    elif block:
        out.br()


def extract_lines(roots, limit: int = MAX_LINES) -> list[str]:
    out = _Lines(limit)
    for root in roots:
        out.br()
        _walk(root, out, 0)
        out.br()
    return out.lines


def apply_ignore(lines: list[str], patterns) -> list[str]:
    if not patterns:
        return lines
    result = []
    for line in lines:
        for pattern in patterns:
            line = pattern.sub(" ", line)
        line = _EMPTY_CELLS.sub(" | ", clean_text(line)).strip(" |")  # tidy cells emptied by a pattern
        if line and has_alnum(line):
            result.append(line)
    return result


def extract_links(roots, base_url: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for root in roots:
        for a in root.iter("a", "area"):
            href = (a.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            if href[:11].lower().startswith(("javascript:", "mailto:", "tel:", "data:", "sms:")):
                continue
            try:
                url = normalize_url(urljoin(base_url, href))
            except ValueError:
                continue
            if not url or url in seen:
                continue
            seen.add(url)
            text = clean_text(a.text_content()) or clean_text(a.get("title") or a.get("aria-label") or "")
            links.append((truncate(text, 300), url))
    return links


def page_snapshot(doc, base_url: str, *, selector: str = "", exclude_selectors=(), ignore_patterns=(),
                  track_links: bool = False, fallback: bool = False) -> Snapshot:
    """Extract the monitored region of a page. Mutates ``doc`` (drops excluded elements)."""
    title = document_title(doc)
    drop_selectors(doc, DEFAULT_EXCLUDES)
    drop_selectors(doc, exclude_selectors)
    roots = select_all(doc, selector) if selector else []
    if selector and not roots and fallback:
        for candidate in FALLBACK_SELECTORS:
            roots = select_all(doc, candidate)
            if roots:
                break
    if selector and not roots and not fallback:
        raise ExtractError(f"selector {selector!r} matched nothing on the page", kind="selector")
    if not roots:
        body = doc.find("body")
        roots = [body if body is not None else doc]
    lines = apply_ignore(extract_lines(roots), ignore_patterns)
    links = extract_links(roots, base_url) if track_links else []
    return Snapshot(lines, links, title)


def extract_items(doc, base_url: str, item_selector: str, link_selector: str = "",
                  exclude_selectors=(), ignore_patterns=()) -> list[Item]:
    """List entries (news headlines, announcements, posts) identified by their link."""
    drop_selectors(doc, exclude_selectors)
    items: list[Item] = []
    seen: set[str] = set()
    for el in select_all(doc, item_selector):
        lines = apply_ignore(extract_lines([el], limit=300), ignore_patterns)
        if not lines:
            continue
        anchor = None
        if link_selector:
            found = _compiled(link_selector)(el)
            anchor = found[0] if found else None
        elif el.tag == "a" and el.get("href"):
            anchor = el
        else:
            for candidate in el.iter("a"):
                href = (candidate.get("href") or "").strip()
                if href and not href.startswith(("#", "javascript:")):
                    anchor = candidate
                    break
        url = None
        if anchor is not None and anchor.get("href"):
            try:
                url = normalize_url(urljoin(base_url, anchor.get("href").strip()))
            except ValueError:
                url = None
        # The headline is usually the item's first link; with an explicit link selector (e.g. a date
        # link on Telegram) or a very short link text, use the first substantial line instead.
        title = clean_text(anchor.text_content()) if anchor is not None and not link_selector else ""
        if len(title) < 3:
            title = next((line for line in lines if len(line) >= 20), lines[0])
        body = [line for line in lines if line != title]
        text = " · ".join(lines)
        item_id = url or "h:" + sha256_text(text)[:20]
        if item_id in seen:
            item_id += "#" + sha256_text(text)[:10]
            if item_id in seen:
                continue
        seen.add(item_id)
        items.append(Item(item_id, truncate(title, 300), url, truncate(" · ".join(body), 3000)))
    return items


# --------------------------------------------------------------------------- feeds

def looks_like_feed(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in ("rss", "atom", "/xml", "+xml")) and "html" not in ct:
        return True
    head = body[:1024].lstrip().lower()
    return head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


def _localname(el) -> str:
    return etree.QName(el).localname.lower() if isinstance(el.tag, str) else ""


def _feed_text(el) -> str:
    if el is None:
        return ""
    if len(el):  # inline XHTML content
        return clean_text(" ".join(el.itertext()))
    raw = el.text or ""
    if "<" in raw and ">" in raw:
        try:
            return clean_text(lxml.html.fragment_fromstring(raw, create_parent="div").text_content())
        except (etree.ParserError, ValueError):
            pass
    return clean_text(raw)


def parse_feed(data: bytes, base_url: str) -> list[Item]:
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, remove_comments=True)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ExtractError(f"invalid RSS/Atom XML: {exc}", kind="parse") from None
    if root is None or _localname(root) not in ("rss", "feed", "rdf", "channel"):
        raise ExtractError("the response is not an RSS/Atom feed", kind="type")
    items: list[Item] = []
    seen: set[str] = set()
    for el in root.iter():
        if _localname(el) not in ("item", "entry"):
            continue
        fields: dict[str, object] = {}
        link = None
        for child in el:
            name = _localname(child)
            if not name:
                continue
            if name == "link":
                href = child.get("href")
                rel = (child.get("rel") or "alternate").lower()
                if href and rel == "alternate" and link is None:
                    link = href.strip()
                elif not href and (child.text or "").strip() and link is None:
                    link = child.text.strip()
            elif name not in fields:
                fields[name] = child
        title = _feed_text(fields.get("title"))
        summary = ""
        for name in ("description", "summary", "content", "encoded"):
            if fields.get(name) is not None:
                summary = _feed_text(fields[name])
                break
        guid = ""
        for name in ("guid", "id"):
            if fields.get(name) is not None:
                guid = clean_text(fields[name].text)
                break
        published = ""
        for name in ("pubdate", "published", "updated", "date"):
            if fields.get(name) is not None:
                published = clean_text(fields[name].text)
                break
        url = None
        if link:
            try:
                url = normalize_url(urljoin(base_url, link))
            except ValueError:
                url = None
        item_id = guid or url or "h:" + sha256_text(title + summary)[:20]
        if not (title or summary) or item_id in seen:
            continue
        seen.add(item_id)
        items.append(Item(item_id, truncate(title or summary, 300), url, truncate(summary, 3000), published))
    return items


def feed_links(doc, base_url: str) -> list[str]:
    """RSS/Atom feeds advertised by an HTML page."""
    result = []
    for link in doc.iter("link"):
        kind = (link.get("type") or "").lower()
        if "rss" in kind or "atom" in kind:
            href = (link.get("href") or "").strip()
            if href:
                url = normalize_url(urljoin(base_url, href))
                if url and url not in result:
                    result.append(url)
    return result


# --------------------------------------------------------------------------- bot walls

_BLOCK_PATTERNS = (
    ("Cloudflare", re.compile(
        r"cf-browser-verification|cf_chl_opt|/cdn-cgi/challenge-platform|<title>\s*Just a moment\.\.\.|"
        r"Attention Required! \| Cloudflare|cf-error-details", re.I)),
    ("Akamai", re.compile(r"errors\.edgesuite\.net|<title>\s*Access Denied\s*</title>", re.I)),
    ("Imperva/Incapsula", re.compile(r"Incapsula incident ID|_Incapsula_Resource", re.I)),
    ("DDoS-Guard", re.compile(r"ddos-guard\.net|<title>\s*DDoS-Guard", re.I)),
    ("Sucuri", re.compile(r"Sucuri WebSite Firewall|sucuri\.net/privacy-policy", re.I)),
    ("DataDome", re.compile(r"captcha-delivery\.com|geo\.captcha", re.I)),
    ("PerimeterX", re.compile(r"_pxCaptcha|px-captcha", re.I)),
    ("AWS WAF", re.compile(r"awswaf|aws-waf-token", re.I)),
    ("CAPTCHA", re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com|challenges\.cloudflare\.com/turnstile", re.I)),
)
_JS_REQUIRED = re.compile(
    r"(enable|turn on|activate)\s+javascript|javascript\s+(is\s+)?(required|disabled|needed)|"
    r"requires\s+javascript|جاوا\s*اسکریپت", re.I)
# Bot walls that a real browser can usually pass by simply waiting a few seconds.
INTERSTITIALS = frozenset({"Cloudflare", "DDoS-Guard", "Imperva/Incapsula", "Sucuri", "AWS WAF", "DataDome",
                           "PerimeterX"})
_MARKUP_RE = re.compile(r"<(script|style|noscript|template)\b.*?</\1\s*>|<!--.*?-->|<[^>]+>", re.S | re.I)


def visible_text_length(html: str) -> int:
    """Rough length of the human-visible text of an HTML document."""
    if not html:
        return 0
    return len(clean_text(_MARKUP_RE.sub(" ", html[:3_000_000])))


def detect_block(html: str, visible_length: int | None = None, *, include_js: bool = True) -> str | None:
    """Name of the bot protection a (small) page looks like, if any."""
    if not html:
        return None
    if visible_length is None:
        visible_length = visible_text_length(html)
    if visible_length > 3000:
        return None
    head = html[:300_000]
    for name, pattern in _BLOCK_PATTERNS:
        if pattern.search(head):
            return name
    if include_js and _JS_REQUIRED.search(head):
        return "JavaScript required"
    return None

"""`inspect` command: analyse a URL and suggest how to monitor it.

Replaces the old inspect_selector.py. It reports whether plain HTTP is enough
or a browser is needed, suggests stable CSS selectors (scored by amount of
real text and low link density), finds repeated news/announcement lists for
"items" watches, and lists advertised RSS/Atom feeds.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from .config import Watch
from .extract import (
    SKIP_TAGS, ExtractError, _compiled, detect_block, document_base, document_title, extract_items, extract_lines,
    feed_links, looks_like_feed, page_snapshot, parse_feed, parse_html, visible_text_length,
)
from .fetchers import BrowserFetcher, FetchError, HttpFetcher
from .util import clean_text, truncate

_DYNAMIC = re.compile(r"\d{3,}|^(css|jsx|sc|svelte|emotion|chakra|tw)-|^_|__[a-z0-9]{4,}$|[a-z]+\d[a-z0-9]{4,}$", re.I)
_STATE_CLASSES = {"active", "selected", "open", "opened", "show", "shown", "hidden", "visible", "current",
                  "collapsed", "expanded", "in", "fade", "disabled", "focus", "hover", "first", "last", "odd", "even"}
_SAFE_IDENT = re.compile(r"^[A-Za-z_-][A-Za-z0-9_-]*$")
_CONTENT_HINT = re.compile(r"content|main|article|body|post|entry|text|news|page|story|detail|inner", re.I)
_CHROME_HINT = re.compile(r"header|nav|menu|footer|sidebar|breadcrumb|cookie|banner|social|share|search", re.I)


def _stable_classes(el) -> list[str]:
    result = []
    for cls in (el.get("class") or "").split():
        low = cls.lower()
        if (low in _STATE_CLASSES or low.startswith(("is-", "has-", "js-")) or _DYNAMIC.search(cls)
                or not _SAFE_IDENT.match(cls)):
            continue
        result.append(cls)
    return result[:3]


def _simple_selector(el) -> str:
    el_id = el.get("id") or ""
    if el_id and _SAFE_IDENT.match(el_id) and not _DYNAMIC.search(el_id):
        return f"#{el_id}"
    return el.tag.lower() + "".join(f".{c}" for c in _stable_classes(el))


def css_selector_for(doc, el, max_depth: int = 5) -> str | None:
    """A short, stable CSS selector for ``el`` (unique when possible)."""
    parts: list[str] = []
    node = el
    while node is not None and isinstance(node.tag, str) and node.tag.lower() not in ("html", "body"):
        parts.insert(0, _simple_selector(node))
        selector = " > ".join(parts)
        try:
            matches = _compiled(selector)(doc)
        except Exception:
            return None
        if len(matches) == 1 and matches[0] is el:
            return selector
        if len(parts) >= max_depth or parts[0].startswith("#"):
            break  # ancestors above an #id cannot make the selector more specific
        node = node.getparent()
    return " > ".join(parts) if parts else None


def _region_weight(el) -> float:
    """Prefer real content containers over page chrome (header, menus, footer)."""
    tag = el.tag.lower()
    names = f"{el.get('id') or ''} {el.get('class') or ''}"
    if tag in ("header", "nav", "footer", "aside") or _CHROME_HINT.search(names):
        return 0.2
    if tag in ("main", "article") or el.get("role") == "main" or re.search(r"\b(main|content)\b", names, re.I):
        return 1.5
    return 1.0


def content_candidates(doc, limit: int = 12) -> list[dict]:
    elements = []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in SKIP_TAGS or tag in ("html", "head", "body", "a", "span", "li", "p"):
            continue
        if (tag in ("main", "article") or el.get("role") == "main" or el.get("id")
                or _CONTENT_HINT.search(el.get("class") or "")):
            elements.append(el)
        if len(elements) >= 400:
            break
    seen: set[str] = set()
    best_by_text: dict[tuple, dict] = {}
    for el in elements:
        selector = css_selector_for(doc, el)
        if not selector or selector in seen:
            continue
        seen.add(selector)
        lines = extract_lines([el], limit=4000)
        length = sum(map(len, lines))
        if length < 120:
            continue
        link_text = sum(len(clean_text(a.text_content())) for a in el.iter("a"))
        density = min(1.0, link_text / max(1, length))
        candidate = {
            "selector": selector, "length": length, "lines": len(lines), "link_density": density,
            "matches": len(_compiled(selector)(doc)), "score": length * (1 - density) ** 2 * _region_weight(el),
            "preview": truncate(" ".join(lines), 110),
        }
        key = (length, candidate["preview"])  # wrappers with identical text: keep the shortest selector
        current = best_by_text.get(key)
        if current is None or (candidate["score"], -len(selector)) > (current["score"], -len(current["selector"])):
            best_by_text[key] = candidate
    results = sorted(best_by_text.values(), key=lambda c: c["score"], reverse=True)
    return results[:limit]


def _signature(el) -> tuple[str, str]:
    classes = _stable_classes(el)
    return el.tag.lower(), classes[0] if classes else ""


def item_candidates(doc, limit: int = 8) -> list[dict]:
    groups: dict[str, dict] = {}
    for parent in doc.iter():
        if not isinstance(parent.tag, str) or parent.tag.lower() in SKIP_TAGS:
            continue
        children = [c for c in parent if isinstance(c.tag, str)]
        if len(children) < 3:
            continue
        for signature, count in Counter(_signature(c) for c in children).items():
            if count < 3:
                continue
            members = [c for c in children if _signature(c) == signature]
            linked = [m for m in members
                      if any((a.get("href") or "").strip() not in ("", "#") for a in m.iter("a"))]
            if len(linked) < 3:
                continue
            texts = [clean_text(m.text_content()) for m in linked]
            average = sum(map(len, texts)) / len(texts)
            if average < 15 or average > 2000:
                continue
            parent_selector = css_selector_for(doc, parent)
            tag, cls = signature
            child = tag + (f".{cls}" if cls else "")
            selector = f"{parent_selector} > {child}" if parent_selector else child
            groups[selector] = {"selector": selector, "count": len(linked), "average": average,
                                "samples": [truncate(t, 90) for t in texts[:3]],
                                "score": len(linked) * min(average, 220)}
    return sorted(groups.values(), key=lambda g: g["score"], reverse=True)[:limit]


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


async def _fetch(config, url: str, use_browser: bool, wait_for: str):
    watch = Watch(id="inspect", url=url, timeout_seconds=45, challenge_wait_seconds=25)
    http, browser = HttpFetcher(config), BrowserFetcher(config)
    results = {}
    try:
        if not use_browser:
            try:
                results["http"] = await http.fetch(url, watch)
            except FetchError as exc:
                print(f"Plain HTTP failed: {exc}")
        needs_browser = use_browser or "http" not in results
        if not needs_browser:
            res = results["http"]
            if res.text and not looks_like_feed(res.content_type, res.body):
                visible = visible_text_length(res.text)
                hint = detect_block(res.text, visible)
                if hint or visible < 300:
                    print(f"Plain HTTP returned little usable text ({visible} chars"
                          + (f", looks like {hint}" if hint else "") + "); trying the browser…")
                    needs_browser = True
        if needs_browser:
            try:
                results["browser"] = await browser.fetch(url, watch, wait_for=wait_for)
            except FetchError as exc:
                print(f"Browser failed: {exc}")
    finally:
        await http.close()
        await browser.close()
    return results


async def inspect(config, url: str, *, use_browser: bool = False, selector: str = "", items: str = "") -> int:
    results = await _fetch(config, url, use_browser, selector or items)
    if not results:
        print("\nCould not load the page with either engine.")
        return 1
    engine = "browser" if "browser" in results else "http"
    res = results[engine]
    _print_header("Fetch")
    for name, r in results.items():
        print(f"{name:>8}: HTTP {r.status}  {r.elapsed:.1f}s  {len(r.text or r.body):,} bytes  final URL: {r.final_url}")
    if "http" in results and "browser" in results and results["http"].text and results["browser"].text:
        h, b = visible_text_length(results["http"].text), visible_text_length(results["browser"].text)
        print(f"visible text — http: {h:,} chars, browser: {b:,} chars")

    body = res.body or (res.text or "").encode("utf-8")
    if looks_like_feed(res.content_type, body):
        items_found = parse_feed(body, res.final_url)
        _print_header(f"RSS/Atom feed — {len(items_found)} items")
        for item in items_found[:10]:
            print(f" • {item.title}\n   {item.url or ''}")
        snippet = {"id": "my_feed", "name": "...", "type": "feed", "url": url, "interval_seconds": 60}
        _print_header("Suggested watch (copy into \"watches\")")
        print(json.dumps(snippet, ensure_ascii=False, indent=2))
        return 0

    if not res.text:
        print(f"\nNot an HTML page (Content-Type: {res.content_type}).")
        return 1
    doc = parse_html(res.text, res.final_url)
    base = document_base(doc, res.final_url)
    print(f"title: {document_title(doc)}")
    hint = detect_block(res.text)
    if hint:
        print(f"WARNING: the page looks like {hint} — content may be incomplete.")
    feeds = feed_links(doc, base)
    if feeds:
        _print_header("RSS/Atom feeds advertised by this page (best for news sites)")
        for feed in feeds:
            print(f" • {feed}")

    if selector:
        try:
            snap = page_snapshot(parse_html(res.text, res.final_url), base, selector=selector, track_links=True)
        except ExtractError as exc:
            print(f"\n{exc}")
            return 1
        _print_header(f"Text monitored by selector {selector!r}: {len(snap.lines)} lines, "
                      f"{snap.text_length:,} chars, {len(snap.links)} links")
        for line in snap.lines[:80]:
            print(f"  {truncate(line, 160)}")
        if len(snap.lines) > 80:
            print(f"  … {len(snap.lines) - 80} more lines")
        print(f"\nTip: set \"min_content_length\" to about {max(30, snap.text_length // 2)} for this selector.")
        return 0

    if items:
        found = extract_items(parse_html(res.text, res.final_url), base, items)
        _print_header(f"Items matched by {items!r}: {len(found)}")
        for item in found[:20]:
            print(f" • {truncate(item.title, 120)}\n   {item.url or '(no link)'}")
        return 0 if found else 1

    candidates = content_candidates(doc)
    _print_header("Content regions (best first) — use as \"selector\"")
    for c in candidates:
        multi = f"  [{c['matches']} matches]" if c["matches"] > 1 else ""
        print(f"  {c['length']:>7,} chars  links {c['link_density']:>4.0%}  {c['selector']}{multi}")
        print(f"           {c['preview']}")
    lists = item_candidates(doc)
    if lists:
        _print_header("Repeated lists (news/announcements) — use as \"item_selector\" with type \"items\"")
        for g in lists:
            print(f"  {g['count']:>3} items  ~{g['average']:.0f} chars  {g['selector']}")
            for sample in g["samples"]:
                print(f"           · {sample}")

    best = candidates[0]["selector"] if candidates else ""
    snippet = {"id": "my_site", "name": document_title(doc)[:60] or "...", "url": url, "selector": best,
               "engine": engine, "interval_seconds": 120, "track_links": True}
    _print_header("Suggested watch (copy into \"watches\" and adjust)")
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    print("\nNext: preview exactly what will be monitored with\n"
          f"  python monitor_system.py inspect \"{url}\" --selector \"{best}\"")
    return 0

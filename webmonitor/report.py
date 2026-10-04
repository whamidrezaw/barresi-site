"""Diffing snapshots and rendering Telegram-ready (HTML) reports.

A report payload is a JSON-serialisable dict::

    {"header": str, "cont": str, "blocks": [str], "full_blocks": [str] | None,
     "doc_title": str | None, "silent": bool}

Every block is a self-contained, tag-balanced HTML snippet, so messages can be
split between blocks without ever producing invalid Telegram HTML. ``blocks``
are shortened for chat; ``full_blocks`` (when different) hold the complete text
and are sent as an HTML file attachment.
"""

from __future__ import annotations

import difflib
from collections import Counter
from dataclasses import dataclass
from datetime import tzinfo

from .util import esc, esc_attr, find_keywords, format_ts, human_duration, truncate

MSG_LINE_LIMIT = 1100     # max chars of one changed line inside a Telegram message
EXCERPT_LINES = 25        # lines shown for a newly discovered page
SEPARATOR = "┈┈┈┈┈┈┈┈"


@dataclass
class Hunk:
    kind: str          # add | remove | modify
    old: str = ""
    new: str = ""
    context: str = ""  # nearest unchanged line before this change


# --------------------------------------------------------------------------- diffing

def _similarity(a: str, b: str) -> float:
    sm = difflib.SequenceMatcher(None, a.split(), b.split(), autojunk=False)
    if sm.real_quick_ratio() < 0.45 or sm.quick_ratio() < 0.45:
        return 0.0
    return sm.ratio()


def _pair_replace(olds: list[str], news: list[str]) -> list[Hunk]:
    """Pair similar old/new lines of a replaced block (in order) to show word-level edits."""
    if len(olds) * len(news) > 3000:
        return [Hunk("remove", old=o) for o in olds] + [Hunk("add", new=n) for n in news]
    pairs: dict[int, int] = {}
    start = 0
    for i, old in enumerate(olds):
        best, best_ratio = None, 0.0
        for j in range(start, len(news)):
            ratio = _similarity(old, news[j])
            if ratio > best_ratio:
                best, best_ratio = j, ratio
        if best is not None and best_ratio >= 0.45:
            pairs[i] = best
            start = best + 1
    hunks: list[Hunk] = []
    j = 0
    for i, old in enumerate(olds):
        if i in pairs:
            while j < pairs[i]:
                hunks.append(Hunk("add", new=news[j]))
                j += 1
            hunks.append(Hunk("modify", old=old, new=news[j]))
            j += 1
        else:
            hunks.append(Hunk("remove", old=old))
    hunks.extend(Hunk("add", new=n) for n in news[j:])
    return hunks


def _drop_moves(hunks: list[Hunk]) -> list[Hunk]:
    """Lines that were only moved (removed here, added there unchanged) are not real changes."""
    moved = Counter(h.old for h in hunks if h.kind == "remove") & Counter(h.new for h in hunks if h.kind == "add")
    if not moved:
        return hunks
    removed_budget, added_budget = moved.copy(), moved.copy()
    result: list[Hunk] = []
    carry = ""
    for h in hunks:
        if h.kind == "remove" and removed_budget[h.old] > 0:
            removed_budget[h.old] -= 1
            carry = carry or h.context
            continue
        if h.kind == "add" and added_budget[h.new] > 0:
            added_budget[h.new] -= 1
            carry = carry or h.context
            continue
        if carry and not h.context:
            h.context = carry
        carry = ""
        result.append(h)
    return result


def diff_lines(old: list[str], new: list[str], ignore_reorder: bool = True) -> list[Hunk]:
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    hunks: list[Hunk] = []
    last_equal = ""
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            last_equal = new[j2 - 1]
            continue
        if tag == "delete":
            group = [Hunk("remove", old=line) for line in old[i1:i2]]
        elif tag == "insert":
            group = [Hunk("add", new=line) for line in new[j1:j2]]
        else:
            group = _pair_replace(old[i1:i2], new[j1:j2])
        if group:
            group[0].context = last_equal
        hunks.extend(group)
    return _drop_moves(hunks) if ignore_reorder else hunks


def diff_links(old: list, new: list) -> tuple[list, list]:
    old_urls = {url for _, url in old}
    new_urls = {url for _, url in new}
    added = [(text, url) for text, url in new if url not in old_urls]
    removed = [(text, url) for text, url in old if url not in new_urls]
    return added, removed


# --------------------------------------------------------------------------- rendering

def word_diff_html(old: str, new: str, budget: int | None = MSG_LINE_LIMIT, context_words: int | None = 10) -> str:
    """Inline word diff: removed words struck through, inserted words bold+underlined.

    With ``budget``/``context_words`` set to None the full line is rendered.
    """
    a, b = old.split(" "), new.split(" ")
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    parts: list[str] = []
    used = 0
    n = context_words
    for index, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            words = a[i1:i2]
            first, last = index == 0, index == len(ops) - 1
            if n is None:
                text = " ".join(words)
            elif first and not last and len(words) > n:
                text = "… " + " ".join(words[-n:])
            elif last and not first and len(words) > n:
                text = " ".join(words[:n]) + " …"
            elif not first and not last and len(words) > 2 * n:
                text = " ".join(words[:n]) + " … " + " ".join(words[-n:])
            else:
                text = " ".join(words)
            part = esc(text)
        else:
            cut = (lambda s: truncate(s, 400)) if budget else (lambda s: s)
            removed = cut(" ".join(a[i1:i2])) if tag in ("delete", "replace") else ""
            added = cut(" ".join(b[j1:j2])) if tag in ("insert", "replace") else ""
            segments = []
            if removed:
                segments.append(f"<s>{esc(removed)}</s>")
            if added:
                segments.append(f"<b><u>{esc(added)}</u></b>")
            part = " ".join(segments)
        if budget and used + len(part) > budget and parts:
            parts.append("…")
            break
        parts.append(part)
        used += len(part) + 1
    return " ".join(parts)


def render_hunk(h: Hunk, full: bool = False) -> str:
    cut = (lambda s: s) if full else (lambda s: truncate(s, MSG_LINE_LIMIT))
    context = f"<i>📍 {esc(truncate(h.context, 90))}</i>\n" if h.context else ""
    if h.kind == "add":
        body = f"🟢 {esc(cut(h.new))}"
    elif h.kind == "remove":
        body = f"🔴 <s>{esc(cut(h.old))}</s>"
    elif full:
        body = f"🟡 {word_diff_html(h.old, h.new, budget=None, context_words=None)}"
    else:
        body = f"🟡 {word_diff_html(h.old, h.new)}"
    return context + body


def render_link(text: str, url: str, icon: str) -> str:
    label = esc(truncate(text or url, 120))
    return f'{icon} <a href="{esc_attr(url)}">{label}</a>'


def _display_url(url: str) -> str:
    return truncate(url.split("://", 1)[-1], 90)


def _header(icon: str, title: str, url: str | None, ts: float, tz: tzinfo, extra: list[str] = ()) -> str:
    lines = [f"{icon} <b>{title}</b>"]
    if url:
        lines.append(f'🔗 <a href="{esc_attr(url)}">{esc(_display_url(url))}</a>')
    lines.append(f"🕒 {esc(format_ts(ts, tz))}")
    lines.extend(line for line in extra if line)
    return "\n".join(lines)


def _payload(header: str, cont: str, blocks: list[str], *, silent: bool, doc_title: str | None,
             full_blocks: list[str] | None = None, attach: bool = False) -> dict:
    """``attach``: send the full report file even if all messages fit (text was shortened)."""
    if full_blocks is not None and full_blocks == blocks:
        full_blocks = None
    return {"header": header, "cont": cont, "blocks": blocks, "full_blocks": full_blocks,
            "doc_title": doc_title, "attach": bool(attach and doc_title), "silent": silent}


def render_document(title: str, header_html: str, blocks: list[str]) -> str:
    """Stand-alone HTML file with the complete report (sent as a Telegram document)."""
    body = "\n".join(f'<div class="b">{block}</div>' for block in blocks)
    return (
        "<!doctype html><html lang=\"fa\" dir=\"rtl\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{esc(title)}</title><style>"
        "body{font-family:Vazirmatn,Tahoma,'Segoe UI',sans-serif;max-width:920px;margin:auto;padding:14px;"
        "line-height:1.9;background:#f6f7f9;color:#1f2328}"
        ".h,.b{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:10px 14px;margin:10px 0;"
        "white-space:pre-wrap;unicode-bidi:plaintext;overflow-wrap:anywhere}"
        ".h{background:#eef4ff}s{color:#b42318;background:#fdecea}b u{color:#067647;background:#e7f6ec;"
        "text-decoration:none}a{color:#1a5fd0}i{color:#667085}"
        "</style></head><body>"
        f'<div class="h">{header_html}</div>{body}</body></html>'
    )


def _keywords_line(hits: list[str]) -> str:
    return f"🔑 <b>کلمات کلیدی:</b> {esc('، '.join(hits))}" if hits else ""


# --------------------------------------------------------------------------- events

def change_event(watch, url: str, hunks: list[Hunk], links_added: list, links_removed: list,
                 ts: float, tz: tzinfo, page_title: str = "") -> tuple[dict, str, list[str]]:
    """Payload, one-line summary and keyword hits for a content change."""
    counts = Counter(h.kind for h in hunks)
    hits = find_keywords(
        watch.keywords,
        *(h.new for h in hunks if h.kind in ("add", "modify")),
        *(text for text, _ in links_added),
    )
    stats = []
    if counts["add"]:
        stats.append(f"🟢 {counts['add']} اضافه")
    if counts["remove"]:
        stats.append(f"🔴 {counts['remove']} حذف")
    if counts["modify"]:
        stats.append(f"🟡 {counts['modify']} ویرایش")
    if links_added:
        stats.append(f"🔗 {len(links_added)} لینک جدید")
    if links_removed:
        stats.append(f"⛓ {len(links_removed)} لینک حذف‌شده")
    extra = []
    if page_title and url != watch.url:
        extra.append(f"📄 {esc(truncate(page_title, 120))}")
    extra.append(" · ".join(stats))
    extra.append(_keywords_line(hits))
    header = _header("🚨" if hits else "🔔", f"تغییر در «{esc(watch.label)}»", url, ts, tz, extra)

    link_blocks = []
    if links_added:
        link_blocks.append("<b>🔗 لینک‌های جدید:</b>")
        link_blocks += [render_link(text, link, "➕") for text, link in links_added]
    if links_removed:
        link_blocks.append("<b>⛓ لینک‌های حذف‌شده:</b>")
        link_blocks += [render_link(text, link, "➖") for text, link in links_removed]
    blocks = [render_hunk(h) for h in hunks] + link_blocks
    full_blocks = [render_hunk(h, full=True) for h in hunks] + link_blocks
    summary = ", ".join(s.split(" ", 1)[1] for s in stats)
    shortened = any(len(h.old) > MSG_LINE_LIMIT or len(h.new) > MSG_LINE_LIMIT for h in hunks)
    payload = _payload(header, f"<i>ادامهٔ تغییرات «{esc(watch.label)}»</i>", blocks,
                       silent=watch.silent and not hits, doc_title=f"تغییرات {watch.label}",
                       full_blocks=full_blocks, attach=shortened)
    return payload, summary, hits


def _line_blocks(lines: list[str], max_chars: int = 1500) -> list[str]:
    """Escape lines and group them into blocks (one line break between lines, not a blank line)."""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line = esc(line)
        if current and size + len(line) > max_chars:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        blocks.append("\n".join(current))
    return blocks


def _published(value: str, tz: tzinfo) -> str:
    """RSS (RFC 822) or Atom (ISO 8601) date in the configured timezone, Jalali first."""
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_ts(dt.timestamp(), tz)


def new_page_event(watch, url: str, snapshot, ts: float, tz: tzinfo) -> tuple[dict, str, list[str]]:
    hits = find_keywords(watch.keywords, snapshot.title, *snapshot.lines)
    title = snapshot.title or url
    extra = [f"📄 <b>{esc(truncate(title, 150))}</b>", _keywords_line(hits)]
    header = _header("🚨" if hits else "🆕", f"صفحهٔ جدید در «{esc(watch.label)}»", url, ts, tz, extra)
    excerpt = _line_blocks([truncate(line, 600) for line in snapshot.lines[:EXCERPT_LINES]])
    if len(snapshot.lines) > EXCERPT_LINES:
        excerpt.append(f"<i>… و {len(snapshot.lines) - EXCERPT_LINES} خط دیگر (متن کامل در فایل پیوست)</i>")
    full = _line_blocks(snapshot.lines)
    shortened = len(snapshot.lines) > EXCERPT_LINES or any(len(line) > 600 for line in snapshot.lines)
    payload = _payload(header, f"<i>ادامهٔ صفحهٔ جدید «{esc(watch.label)}»</i>", excerpt,
                       silent=watch.silent and not hits, doc_title=title, full_blocks=full, attach=shortened)
    return payload, f"new page: {truncate(title, 80)}", hits


def removed_page_event(watch, url: str, title: str, ts: float, tz: tzinfo) -> tuple[dict, str]:
    extra = [f"📄 {esc(truncate(title, 150))}" if title else "", "این صفحه دیگر در دسترس نیست (404/410)."]
    header = _header("🗑", f"صفحه‌ای از «{esc(watch.label)}» حذف شد", url, ts, tz, extra)
    return _payload(header, "", [], silent=watch.silent, doc_title=None), f"page removed: {truncate(title or url, 80)}"


def file_event(watch, url: str, kind: str, info: dict, ts: float, tz: tzinfo) -> tuple[dict, str]:
    name = url.rstrip("/").rsplit("/", 1)[-1] or url
    if kind == "new":
        title = f"فایل جدید در «{esc(watch.label)}»"
    else:
        title = f"فایل «{esc(truncate(name, 60))}» در «{esc(watch.label)}» تغییر کرد"
    extra = [f'📎 <a href="{esc_attr(url)}">{esc(truncate(name, 120))}</a>']
    if info.get("old_size") is not None and info.get("size") is not None:
        extra.append(f"حجم: {info['old_size']:,} ← {info['size']:,} بایت")
    elif info.get("size") is not None:
        extra.append(f"حجم: {info['size']:,} بایت")
    if info.get("content_type"):
        extra.append(f"نوع: {esc(info['content_type'])}")
    header = _header("📄", title, None, ts, tz, extra)
    return _payload(header, "", [], silent=watch.silent, doc_title=None), f"file {kind}: {truncate(name, 80)}"


def new_items_event(watch, items: list, ts: float, tz: tzinfo, hits: list[str]) -> tuple[dict, str]:
    count = len(items)
    title = f"{count} مورد جدید در «{esc(watch.label)}»" if count > 1 else f"مورد جدید در «{esc(watch.label)}»"
    header = _header("🚨" if hits else "📰", title, watch.url, ts, tz, [_keywords_line(hits)])
    blocks, full = [], []
    shortened = False
    for item in items:
        if item.url:
            title_html = f'<a href="{esc_attr(item.url)}"><b>{esc(truncate(item.title, 250))}</b></a>'
        else:
            title_html = f"<b>{esc(truncate(item.title, 250))}</b>"
        body = item.text
        if body.startswith(item.title):
            body = body[len(item.title):].lstrip(" ·")
        when = f"\n<i>🕒 {esc(_published(item.published, tz))}</i>" if item.published else ""
        shortened = shortened or len(body) > 500
        blocks.append(f"▪️ {title_html}" + (f"\n{esc(truncate(body, 500))}" if body else "") + when)
        full.append(f"▪️ {title_html}" + (f"\n{esc(body)}" if body else "") + when)
    payload = _payload(header, f"<i>ادامهٔ موارد جدید «{esc(watch.label)}»</i>", blocks,
                       silent=watch.silent and not hits, doc_title=f"موارد جدید {watch.label}", full_blocks=full,
                       attach=shortened)
    return payload, f"{count} new item(s): {truncate(items[0].title, 70)}"


def error_event(watch, error: str, count: int, hint: str, ts: float, tz: tzinfo) -> tuple[dict, str]:
    extra = [f"❌ {count} بار پیاپی ناموفق بود.", f"<b>خطا:</b> <code>{esc(truncate(error, 400))}</code>"]
    if hint:
        extra.append(f"💡 {hint}")
    extra.append("سیستم به تلاش ادامه می‌دهد و پس از رفع مشکل خبر می‌دهد.")
    header = _header("⚠️", f"مشکل در بررسی «{esc(watch.label)}»", watch.url, ts, tz, extra)
    return _payload(header, "", [], silent=False, doc_title=None), f"error x{count}: {truncate(error, 100)}"


def recovered_event(watch, down_seconds: float, ts: float, tz: tzinfo) -> tuple[dict, str]:
    extra = [f"مدت اختلال: {human_duration(down_seconds)}"]
    header = _header("✅", f"«{esc(watch.label)}» دوباره با موفقیت بررسی شد", watch.url, ts, tz, extra)
    return _payload(header, "", [], silent=True, doc_title=None), "recovered"


def info_event(header_html: str, blocks: list[str] | None = None, silent: bool = True) -> dict:
    return _payload(header_html, "", list(blocks or []), silent=silent, doc_title=None)


def digest_payload(watch, payloads: list[dict], first_ts: float, last_ts: float) -> dict:
    """Merge several pending payloads of one watch (used while a cooldown is active)."""
    header = (f"🔔 <b>{len(payloads)} رویداد در «{esc(watch.label)}»</b>\n"
              f"⏳ طی {human_duration(last_ts - first_ts)} گذشته (حالت تجمیع / cooldown)")
    blocks: list[str] = []
    full_blocks: list[str] = []
    for index, payload in enumerate(payloads, 1):
        marker = f"<b>{SEPARATOR} {index} {SEPARATOR}</b>\n{payload['header']}"
        blocks.append(marker)
        blocks.extend(payload["blocks"])
        full_blocks.append(marker)
        full_blocks.extend(payload.get("full_blocks") or payload["blocks"])
    return _payload(header, f"<i>ادامهٔ رویدادهای «{esc(watch.label)}»</i>", blocks,
                    silent=all(p.get("silent") for p in payloads), doc_title=f"رویدادهای {watch.label}",
                    full_blocks=full_blocks, attach=any(p.get("attach") for p in payloads))

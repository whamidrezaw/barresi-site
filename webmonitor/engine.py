"""The monitoring engine: schedules checks, detects changes and delivers notifications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import report
from .extract import (
    ASSET_EXTENSIONS, FILE_EXTENSIONS, ExtractError, Snapshot, detect_block, document_base, extract_items,
    extract_links, looks_like_feed, normalize_url, page_snapshot, parse_feed, parse_html, same_site, url_extension,
)
from .fetchers import MAX_FILE_BYTES, BrowserFetcher, FetchError, HttpFetcher
from .notifier import (
    ConsoleNotifier, NotifyError, TelegramApiError, TelegramNotifier, build_parts, explain_telegram_error,
)
from .storage import Store
from .util import esc, find_keywords, format_ts, human_duration, load_timezone, sha256_text, truncate

log = logging.getLogger("webmonitor")

EXTRACTOR_VERSION = 1           # bump to silently re-baseline after extraction changes
DIGESTABLE = {"change", "new_page", "removed_page", "file", "new_items"}
SWITCH_STATUSES = {401, 403, 406, 429, 503}
SYSTEM = "_system"
_CRAWL_SKIP = re.compile(r"/(log-?out|sign-?out|log-?off)\b|[?&](print|share|replytocom|format)=", re.I)
# Telegram 400 errors caused by the message itself (retrying the same message cannot help)
_CONTENT_ERROR = re.compile(r"too long|parse|entit|text is empty|caption|file|document", re.I)
TYPE_LABELS = {"page": "صفحه", "items": "فهرست اخبار/اطلاعیه‌ها", "feed": "RSS"}


def watch_fingerprint(w) -> str:
    """Hash of the settings that shape a snapshot; when it changes we re-baseline silently."""
    parts = [EXTRACTOR_VERSION, w.type, w.selector, w.item_selector, w.item_link_selector, w.exclude_selectors,
             [p.pattern for p in w.ignore_patterns], w.track_links, w.crawl.selector if w.crawl else ""]
    return sha256_text(json.dumps(parts, ensure_ascii=False))[:16]


def crawl_fingerprint(w) -> str:
    c = w.crawl
    parts = [w.url, c.max_pages, c.max_depth, c.same_prefix, [p.pattern for p in c.include],
             [p.pattern for p in c.exclude], c.track_files]
    return sha256_text(json.dumps(parts))[:16]


def _browser_may_help(exc: Exception) -> bool:
    if isinstance(exc, ExtractError):
        return exc.kind in ("selector", "short", "empty")
    if isinstance(exc, FetchError):
        return exc.kind in ("blocked", "ssl") or (exc.kind == "http" and exc.status in SWITCH_STATUSES)
    return False


def _cache_busted(url: str) -> str:
    parts = urlsplit(url)
    extra = urlencode({"_wm": int(time.time())})
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class Monitor:
    def __init__(self, config, *, dry_run: bool = False):
        self.config = config
        self.tz = load_timezone(config.timezone)
        self.store = Store(config.data_dir / "monitor.db")
        self.http = HttpFetcher(config)
        self.browser = BrowserFetcher(config)
        if config.telegram.enabled and not dry_run:
            self.notifier = TelegramNotifier(config.telegram)
            self.chat_ids = list(config.telegram.chat_ids)
        else:
            self.notifier = ConsoleNotifier(config.data_dir / "reports")
            self.chat_ids = ["console"]
        self.watches = [w for w in config.watches if w.enabled]
        self._by_id = {w.id: w for w in config.watches}
        self.started_at = time.time()
        self._stop = asyncio.Event()
        self._outbox = asyncio.Event()
        self._triggers = {w.id: asyncio.Event() for w in self.watches}
        self._crawl_wake = {w.id: asyncio.Event() for w in self.watches if w.crawl}
        self._crawl_pending: dict[str, set] = {w.id: set() for w in self.watches if w.crawl}
        self._root_links: dict[str, list] = {}
        self._root_final: dict[str, str] = {}
        self._stats: dict[str, Counter] = defaultdict(Counter)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.http.start()
        await self.notifier.start()
        stale = self.store.forget_unknown_watches([w.id for w in self.config.watches])
        if stale:
            log.info("Removed stored state of watches no longer in the config: %s", ", ".join(stale))

    async def close(self) -> None:
        for closer in (self.browser.close, self.http.close, self.notifier.close):
            try:
                await closer()
            except Exception:
                log.debug("error while closing", exc_info=True)
        await asyncio.sleep(0.25)  # let aiohttp finish closing SSL transports (avoids warnings on Windows)
        self.store.close()

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        if self.config.notify_startup:
            self._enqueue_info(*self._startup_report())
        tasks = [asyncio.create_task(self._watch_loop(w), name=f"watch:{w.id}") for w in self.watches]
        tasks += [asyncio.create_task(self._crawl_loop(w), name=f"crawl:{w.id}") for w in self.watches if w.crawl]
        tasks.append(asyncio.create_task(self._dispatch_loop(), name="dispatch"))
        tasks.append(asyncio.create_task(self._housekeeping_loop(), name="housekeeping"))
        if isinstance(self.notifier, TelegramNotifier) and self.config.telegram.commands:
            tasks.append(asyncio.create_task(self._command_loop(), name="commands"))
        log.info("Monitoring %d watch(es); notifications via %s", len(self.watches), self.notifier.name)
        try:
            await self._stop.wait()
        finally:
            log.info("Shutting down…")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(self.dispatch_pending(), timeout=15)
            except Exception:
                pass

    # ------------------------------------------------------------------ scheduling

    async def _watch_loop(self, w) -> None:
        await asyncio.sleep(random.uniform(0.5, min(8.0, max(1.0, w.interval_seconds / 2))))
        trigger = self._triggers[w.id]
        while True:
            trigger.clear()
            await self.check_watch(w)
            delay = w.interval_seconds + random.uniform(0, w.jitter_seconds)
            try:
                await asyncio.wait_for(trigger.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def trigger_all(self) -> None:
        for event in self._triggers.values():
            event.set()

    async def check_watch(self, w) -> str:
        """Run one check of a watch. Never raises (except cancellation)."""
        self._stats[w.id]["checks"] += 1
        started = time.monotonic()
        try:
            if w.type == "page":
                result = await self._check_page(w)
            elif w.type == "items":
                result = await self._check_items(w)
            else:
                result = await self._check_feed(w)
        except (FetchError, ExtractError) as exc:
            return self._on_failure(w, exc)
        except Exception as exc:
            log.exception("[%s] unexpected error", w.id)
            return self._on_failure(w, exc)
        self._on_success(w)
        log.info("[%s] %s (%.1fs)", w.id, result, time.monotonic() - started)
        return result

    def _on_failure(self, w, exc: Exception) -> str:
        message = str(exc) or type(exc).__name__
        state = self.store.record_check_error(w.id, message)
        count = state.get("consecutive_errors", 1)
        self._stats[w.id]["errors"] += 1
        log.warning("[%s] check failed (%d in a row): %s", w.id, count, message)
        if count >= w.error_threshold and not state.get("alerted"):
            payload, summary = report.error_event(w, message, count, self._error_hint(exc), time.time(), self.tz)
            self.store.add_event(w.id, w.url, "error", summary, payload)
            self.store.update_watch(w.id, alerted=1)
            self._outbox.set()
        return f"error: {message}"

    def _on_success(self, w) -> None:
        previous = self.store.record_check_ok(w.id)
        if previous.get("alerted"):
            down = time.time() - (previous.get("first_error_at") or time.time())
            payload, summary = report.recovered_event(w, down, time.time(), self.tz)
            self.store.add_event(w.id, w.url, "recovered", summary, payload)
            self._outbox.set()
        elif previous.get("consecutive_errors"):
            log.info("[%s] recovered after %d failed check(s)", w.id, previous["consecutive_errors"])

    @staticmethod
    def _error_hint(exc: Exception) -> str:
        if isinstance(exc, ExtractError) and exc.kind in ("selector", "short"):
            return ("احتمالاً ساختار صفحه عوض شده یا سایت ربات را مسدود کرده؛ "
                    "selector را با دستور inspect بازبینی کنید.")
        if isinstance(exc, FetchError):
            if exc.kind == "blocked":
                return "سایت درخواست‌ها را مسدود کرده؛ engine را browser بگذارید یا از proxy استفاده کنید."
            if exc.kind == "timeout":
                return "سایت کند یا از دسترس خارج است؛ در صورت تکرار timeout_seconds را بیشتر کنید."
            if exc.kind == "ssl":
                return "مشکل گواهی SSL سایت؛ engine را browser بگذارید یا verify_ssl را false کنید."
            if exc.kind == "browser":
                return "مرورگر اجرا نشد؛ دستور  patchright install chromium  را اجرا کنید."
            if exc.status in (404, 410):
                return "این آدرس دیگر وجود ندارد؛ URL را بررسی کنید."
        return ""

    # ------------------------------------------------------------------ fetching

    def engine_for(self, w) -> str:
        if w.engine != "auto":
            return w.engine
        return self.store.watch_state(w.id).get("engine") or "http"

    async def _fetch(self, w, url: str, engine: str, *, conditional=None, wait_for: str = ""):
        if engine == "browser":
            return await self.browser.fetch(url, w, wait_for=wait_for)
        return await self.http.fetch(url, w, conditional=conditional)

    async def _fetch_parse(self, w, url: str, parse, *, conditional=None, wait_for: str = "",
                           allow_switch: bool = True):
        """Fetch and parse; in auto mode fall back to (and stick with) the browser when HTTP is not enough."""
        engine = self.engine_for(w)
        try:
            result = await self._fetch(w, url, engine, conditional=conditional, wait_for=wait_for)
            if result.not_modified:
                return result, None
            return result, parse(result)
        except (FetchError, ExtractError) as exc:
            if not (allow_switch and w.engine == "auto" and engine == "http" and _browser_may_help(exc)
                    and self.browser.available):
                raise
            log.info("[%s] plain HTTP was not enough (%s); trying the browser", w.id, exc)
        result = await self.browser.fetch(url, w, wait_for=wait_for)
        parsed = parse(result)
        self.store.update_watch(w.id, engine="browser")
        log.info("[%s] switched to the browser engine for future checks", w.id)
        return result, parsed

    # ------------------------------------------------------------------ page watches

    def _root(self, w) -> str:
        return normalize_url(w.url) or w.url

    def _parse_page(self, w, result, *, subpage: bool = False):
        if not result.text:
            raise ExtractError(f"not an HTML page (Content-Type: {result.content_type or 'unknown'})", kind="type")
        doc = parse_html(result.text, result.final_url)
        base = document_base(doc, result.final_url)
        discovered = extract_links([doc], base) if w.crawl else []
        selector = (w.crawl.selector or w.selector) if subpage else w.selector
        try:
            snap = page_snapshot(doc, base, selector=selector, exclude_selectors=w.exclude_selectors,
                                 ignore_patterns=w.ignore_patterns, track_links=w.track_links, fallback=subpage)
        except ExtractError as exc:
            hint = detect_block(result.text)
            if hint:
                raise ExtractError(f"{exc} — the page looks like {hint}", kind=exc.kind) from None
            raise
        minimum = 1 if subpage else w.min_content_length
        if snap.text_length < minimum:
            hint = detect_block(result.text)
            raise ExtractError(
                f"content too short ({snap.text_length} chars, expected at least {minimum})"
                + (f" — the page looks like {hint}" if hint else ""), kind="short")
        return snap, discovered

    async def _check_page(self, w) -> str:
        root = self._root(w)
        prev = self.store.get_page(w.id, root)
        conditional = None
        if prev is not None and prev["snapshot"] and prev["fingerprint"] == watch_fingerprint(w) and not w.cache_bust:
            conditional = {"etag": prev["etag"], "last_modified": prev["last_modified"]}
        url = _cache_busted(w.url) if w.cache_bust else w.url
        result, parsed = await self._fetch_parse(w, url, lambda r: self._parse_page(w, r),
                                                 conditional=conditional, wait_for=w.selector)
        if result.not_modified:
            self.store.touch_page(w.id, root)
            return "no change (HTTP 304)"
        snap, discovered = parsed
        if w.crawl:
            final = normalize_url(result.final_url)
            if final and not w.cache_bust:
                self._root_final[w.id] = final
            self._root_links[w.id] = discovered
            self._queue_unknown_links(w, discovered)
        return self._apply_snapshot(w, root, prev, snap, result)

    def _apply_snapshot(self, w, url: str, prev, snap: Snapshot, result) -> str:
        fields = dict(kind="page", digest=snap.digest, snapshot=snap.to_json(), title=snap.title,
                      engine=result.engine, fingerprint=watch_fingerprint(w), etag=result.etag,
                      last_modified=result.last_modified)
        reason = None
        if prev is None or not prev["snapshot"]:
            reason = "first check"
        elif prev["fingerprint"] != fields["fingerprint"]:
            reason = "settings changed"
        elif prev["engine"] and prev["engine"] != result.engine:
            reason = f"engine is now {result.engine}"
        if reason:
            self.store.record_page(w.id, url, **fields)
            links = f", {len(snap.links)} links" if w.track_links else ""
            return f"baseline saved ({reason}): {len(snap.lines)} lines, {snap.text_length} chars{links}"
        if prev["digest"] == fields["digest"]:
            self.store.touch_page(w.id, url, etag=result.etag, last_modified=result.last_modified)
            return "no change"
        old = Snapshot.from_json(prev["snapshot"])
        hunks = report.diff_lines(old.lines, snap.lines, w.ignore_reorder)
        added, removed = report.diff_links(old.links, snap.links) if w.track_links else ([], [])
        if not hunks and not added and not removed:
            self.store.record_page(w.id, url, **fields)
            return "content only re-ordered (ignored)"
        payload, summary, hits = report.change_event(w, url, hunks, added, removed, time.time(), self.tz, snap.title)
        status = "filtered" if w.only_keywords and not hits else "pending"
        self.store.record_page(w.id, url, changed=True, event=("change", summary, payload, status), **fields)
        self._stats[w.id]["changes"] += 1
        self._outbox.set()
        note = f" [keywords: {', '.join(hits)}]" if hits else ""
        if status == "filtered":
            note += " [not sent: no keyword]"
        return f"CHANGE detected: {summary}{note}"

    # ------------------------------------------------------------------ items & feeds

    def _parse_items(self, w, result):
        if not result.text:
            raise ExtractError(f"not an HTML page (Content-Type: {result.content_type or 'unknown'})", kind="type")
        doc = parse_html(result.text, result.final_url)
        items = extract_items(doc, document_base(doc, result.final_url), w.item_selector, w.item_link_selector,
                              w.exclude_selectors, w.ignore_patterns)
        if not items:
            hint = detect_block(result.text)
            raise ExtractError(f"item_selector {w.item_selector!r} matched no items"
                               + (f" — the page looks like {hint}" if hint else ""), kind="selector")
        return items

    def _parse_feed(self, w, result):
        body = result.body or result.text.encode("utf-8")
        if not looks_like_feed(result.content_type, body):
            raise ExtractError(f"not an RSS/Atom feed (Content-Type: {result.content_type or 'unknown'})", kind="type")
        items = parse_feed(body, result.final_url)
        if not items:
            raise ExtractError("the feed contains no items", kind="empty")
        return items

    async def _check_items(self, w) -> str:
        _, items = await self._fetch_parse(w, w.url, lambda r: self._parse_items(w, r), wait_for=w.item_selector)
        return self._apply_items(w, items)

    async def _check_feed(self, w) -> str:
        _, items = await self._fetch_parse(w, w.url, lambda r: self._parse_feed(w, r))
        return self._apply_items(w, items)

    def _apply_items(self, w, items: list) -> str:
        fingerprint = watch_fingerprint(w)
        ids = [item.id for item in items]
        if self.store.watch_state(w.id).get("fingerprint") != fingerprint or not self.store.has_items(w.id):
            self.store.record_items(w.id, ids, reset=True, fingerprint=fingerprint)
            return f"baseline saved: {len(items)} items"
        seen = self.store.seen_ids(w.id)
        new = [item for item in items if item.id not in seen]
        if not new:
            self.store.record_items(w.id, ids)
            return f"no new items ({len(items)} listed)"
        matching = [item for item in new if find_keywords(w.keywords, item.title, item.text)]
        hits = find_keywords(w.keywords, *(f"{item.title}\n{item.text}" for item in new))
        shown = matching if w.only_keywords else new
        status = "pending" if shown else "filtered"
        payload, summary = report.new_items_event(w, shown or new, time.time(), self.tz, hits)
        self.store.record_items(w.id, ids, event=("new_items", summary, payload, status))
        self._stats[w.id]["changes"] += 1
        if status == "pending":
            self._outbox.set()
        note = " [not sent: no keyword]" if status == "filtered" else ""
        return f"NEW: {summary}{note}"

    # ------------------------------------------------------------------ crawling

    def _crawl_kind(self, w, url: str) -> str | None:
        """'page' or 'file' when ``url`` is inside the crawl scope of ``w``."""
        parts, root = urlsplit(url), urlsplit(w.url)
        if not same_site(parts.hostname, root.hostname) or _CRAWL_SKIP.search(url):
            return None
        extension = url_extension(url)
        if extension in ASSET_EXTENSIONS or any(p.search(url) for p in w.crawl.exclude):
            return None
        if extension in FILE_EXTENSIONS:
            return "file" if w.crawl.track_files else None
        if w.crawl.include:
            return "page" if any(p.search(url) for p in w.crawl.include) else None
        if w.crawl.same_prefix:
            prefix = root.path.rsplit("/", 1)[0] + "/"
            if not (parts.path or "/").startswith(prefix):
                return None
        return "page"

    def _root_aliases(self, w) -> set[str]:
        aliases = {self._root(w)}
        if w.id in self._root_final:
            aliases.add(self._root_final[w.id])
        return aliases

    def _scoped(self, w, links: list, visited: set) -> list[tuple[str, str]]:
        result = []
        for _, url in links:
            if url in visited:
                continue
            visited.add(url)
            kind = self._crawl_kind(w, url)
            if kind:
                result.append((url, kind))
        return result

    def _queue_unknown_links(self, w, links: list) -> None:
        state = self.store.watch_state(w.id)
        if not state.get("last_crawl") or state.get("crawl_fingerprint") != crawl_fingerprint(w):
            return  # the first full crawl will pick everything up
        known = self.store.page_urls(w.id) | self._root_aliases(w)
        fresh = {url for url, _ in self._scoped(w, links, set(known))}
        if fresh - self._crawl_pending[w.id]:
            self._crawl_pending[w.id] |= fresh
            self._crawl_wake[w.id].set()

    async def _crawl_loop(self, w) -> None:
        wake, pending = self._crawl_wake[w.id], self._crawl_pending[w.id]
        await asyncio.sleep(random.uniform(10, 20))  # let the start page get its baseline first
        while True:
            state = self.store.watch_state(w.id)
            last = state.get("last_crawl") or 0
            stale = state.get("crawl_fingerprint") != crawl_fingerprint(w)
            due = last + w.crawl.interval_seconds
            if time.time() < due and not pending and not stale:
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=due - time.time())
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if time.time() >= due or stale:
                    pending.clear()
                    await self.crawl(w)
                else:
                    urls = sorted(pending)
                    pending.clear()
                    await self._crawl_new_urls(w, urls)
            except Exception:
                log.exception("[%s] crawl failed", w.id)
                await asyncio.sleep(60)

    async def crawl(self, w) -> str:
        """Full crawl: check every tracked page/file and discover new ones within the scope."""
        started = time.time()
        state = self.store.watch_state(w.id)
        fingerprint = crawl_fingerprint(w)
        baseline = not state.get("last_crawl") or state.get("crawl_fingerprint") != fingerprint
        aliases = self._root_aliases(w)
        known = {url: row for url, row in self.store.page_rows(w.id).items() if url not in aliases}
        links = self._root_links.get(w.id)
        if links is None:
            try:
                _, (_, links) = await self._fetch_parse(w, w.url, lambda r: self._parse_page(w, r),
                                                        wait_for=w.selector, allow_switch=False)
            except (FetchError, ExtractError) as exc:
                log.warning("[%s] crawl skipped, start page failed: %s", w.id, exc)
                return "crawl skipped (start page failed)"
        visited = set(aliases)
        frontier = self._scoped(w, links, visited)
        tracked = set(known)
        sem = asyncio.Semaphore(w.crawl.concurrency)
        stats: Counter = Counter()
        depth = 1
        while frontier and depth <= w.crawl.max_depth:
            batch = []
            for url, kind in frontier:
                if url not in tracked:
                    if len(tracked) >= w.crawl.max_pages:
                        stats["over_limit"] += 1
                        continue
                    tracked.add(url)
                batch.append((url, kind))
            found = await asyncio.gather(*(self._crawl_visit(w, url, kind, known.get(url), baseline, sem, stats)
                                           for url, kind in batch))
            frontier = []
            if depth < w.crawl.max_depth:
                for page_links in found:
                    frontier += self._scoped(w, page_links, visited)
            depth += 1
        leftovers = [(url, row["kind"]) for url, row in known.items() if url not in visited]
        if leftovers:
            await asyncio.gather(*(self._crawl_visit(w, url, kind, known.get(url), baseline, sem, stats, discover=False)
                                   for url, kind in leftovers))
        self.store.update_watch(w.id, last_crawl=time.time(), crawl_fingerprint=fingerprint)
        summary = (f"{stats['checked']} checked, {stats['new']} new, {stats['changed']} changed, "
                   f"{stats['removed']} removed, {stats['errors']} errors")
        if stats["over_limit"]:
            summary += f", {stats['over_limit']} skipped (max_pages={w.crawl.max_pages})"
        log.info("[%s] crawl %s in %.0fs: %s", w.id, "baseline" if baseline else "done", time.time() - started, summary)
        return f"crawl: {summary}"

    async def _crawl_new_urls(self, w, urls: list[str]) -> None:
        """Pages that just appeared in the start page's links: check them right away."""
        known = self.store.page_urls(w.id)
        sem = asyncio.Semaphore(w.crawl.concurrency)
        stats: Counter = Counter()
        todo = []
        for url in urls:
            kind = self._crawl_kind(w, url)
            if url in known or not kind:
                continue
            if len(known) >= w.crawl.max_pages + 1:
                log.warning("[%s] max_pages=%d reached; new page not tracked: %s", w.id, w.crawl.max_pages, url)
                continue
            known.add(url)
            todo.append((url, kind))
        if todo:
            log.info("[%s] %d new link(s) on the start page; checking them now", w.id, len(todo))
            await asyncio.gather(*(self._crawl_visit(w, url, kind, None, False, sem, stats, discover=False)
                                   for url, kind in todo))

    async def _crawl_visit(self, w, url: str, kind: str, prev, baseline: bool, sem, stats: Counter,
                           discover: bool = True) -> list:
        async with sem:
            stats["checked"] += 1
            try:
                if kind == "file":
                    await self._check_file(w, url, prev, baseline, stats)
                    return []
                conditional = None
                if prev is not None and prev["snapshot"] and prev["fingerprint"] == watch_fingerprint(w):
                    conditional = {"etag": prev["etag"], "last_modified": prev["last_modified"]}
                result = await self._fetch(w, url, self.engine_for(w), conditional=conditional)
            except FetchError as exc:
                self._crawl_failure(w, url, prev, exc, stats)
                return []
            if result.not_modified:
                self.store.touch_page(w.id, url)
                return []
            if not result.text and result.body:  # a document served without a file extension
                self._apply_file(w, url, prev, result, baseline, stats)
                return []
            try:
                snap, links = self._parse_page(w, result, subpage=True)
            except ExtractError as exc:
                log.debug("[%s] skipped %s: %s", w.id, url, exc)
                return []
            if prev is None or not prev["snapshot"]:
                event = None
                if not baseline:
                    payload, summary, hits = report.new_page_event(w, url, snap, time.time(), self.tz)
                    event = ("new_page", summary, payload, "filtered" if w.only_keywords and not hits else "pending")
                    stats["new"] += 1
                    self._outbox.set()
                    log.info("[%s] NEW page: %s", w.id, url)
                self.store.record_page(w.id, url, kind="page", digest=snap.digest, snapshot=snap.to_json(),
                                       title=snap.title, engine=result.engine, fingerprint=watch_fingerprint(w),
                                       etag=result.etag, last_modified=result.last_modified, event=event)
            else:
                outcome = self._apply_snapshot(w, url, prev, snap, result)
                if outcome.startswith("CHANGE"):
                    stats["changed"] += 1
                    log.info("[%s] %s on %s", w.id, outcome, url)
            return links if discover else []

    def _crawl_failure(self, w, url: str, prev, exc: FetchError, stats: Counter) -> None:
        stats["errors"] += 1
        if prev is None:
            log.debug("[%s] cannot fetch %s: %s", w.id, url, exc)
            return
        failures = self.store.page_failed(w.id, url)
        if exc.status in (404, 410) and failures >= 2:
            payload, summary = report.removed_page_event(w, url, prev["title"] or "", time.time(), self.tz)
            self.store.delete_page(w.id, url, event=("removed_page", summary, payload, "pending"))
            stats["removed"] += 1
            self._outbox.set()
            log.info("[%s] page REMOVED: %s", w.id, url)
        else:
            log.info("[%s] cannot fetch %s (%d in a row): %s", w.id, url, failures, exc)

    async def _check_file(self, w, url: str, prev, baseline: bool, stats: Counter) -> None:
        conditional = {"etag": prev["etag"], "last_modified": prev["last_modified"]} if prev is not None else None
        result = await self.http.fetch(url, w, conditional=conditional, max_bytes=MAX_FILE_BYTES)
        if result.not_modified:
            self.store.touch_page(w.id, url)
            return
        self._apply_file(w, url, prev, result, baseline, stats)

    def _apply_file(self, w, url: str, prev, result, baseline: bool, stats: Counter) -> None:
        content = result.body or result.text.encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        fields = dict(kind="file", digest=digest, title=url.rsplit("/", 1)[-1], engine="http", fingerprint="file",
                      etag=result.etag, last_modified=result.last_modified, size=len(content))
        info = {"size": len(content), "content_type": result.content_type.split(";")[0].strip()}
        if prev is None or prev["kind"] != "file":
            event = None
            if not baseline:
                payload, summary = report.file_event(w, url, "new", info, time.time(), self.tz)
                event = ("file", summary, payload, "pending")
                stats["new"] += 1
                self._outbox.set()
                log.info("[%s] NEW file: %s", w.id, url)
            self.store.record_page(w.id, url, event=event, **fields)
        elif prev["digest"] != digest:
            info["old_size"] = prev["size"]
            payload, summary = report.file_event(w, url, "changed", info, time.time(), self.tz)
            self.store.record_page(w.id, url, changed=True, event=("file", summary, payload, "pending"), **fields)
            stats["changed"] += 1
            self._outbox.set()
            log.info("[%s] file CHANGED: %s", w.id, url)
        else:
            self.store.touch_page(w.id, url, etag=result.etag, last_modified=result.last_modified)

    # ------------------------------------------------------------------ delivery

    def _enqueue_info(self, header: str, blocks: list[str] | None = None, silent: bool = True) -> None:
        summary = re.sub(r"<[^>]+>", "", header.split("\n", 1)[0])
        self.store.add_event(SYSTEM, None, "info", summary, report.info_event(header, blocks, silent))
        self._outbox.set()

    async def _dispatch_loop(self) -> None:
        while True:
            self._outbox.clear()
            try:
                await self.dispatch_pending()
            except Exception:
                log.exception("notification dispatcher failed")
            try:
                await asyncio.wait_for(self._outbox.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def dispatch_pending(self) -> int:
        """Deliver due events in order (per watch); cooldown watches get one digest per window."""
        now = time.time()
        groups: dict[str, list] = {}
        for row in self.store.pending_events(now):
            groups.setdefault(row["watch_id"], []).append(row)
        delivered = 0
        for watch_id, events in groups.items():
            w = self._by_id.get(watch_id)
            cooldown = w.cooldown_minutes * 60 if w else 0
            if cooldown <= 0:
                for event in events:
                    if not await self._deliver([event], w):
                        break
                    delivered += 1
                continue
            for event in [e for e in events if e["kind"] not in DIGESTABLE]:
                if not await self._deliver([event], w):
                    break
                delivered += 1
            batch = [e for e in events if e["kind"] in DIGESTABLE]
            last = self.store.watch_state(watch_id).get("last_notified") or 0
            if batch and now - last >= cooldown and await self._deliver(batch, w):
                delivered += len(batch)
        return delivered

    async def _deliver(self, events: list, w) -> bool:
        payloads = [json.loads(event["payload"]) for event in events]
        payload = payloads[0] if len(events) == 1 else report.digest_payload(
            w, payloads, events[0]["created"], events[-1]["created"])
        parts = build_parts(payload)
        single = len(events) == 1
        progress = set(json.loads(events[0]["progress"] or "[]")) if single else set()
        failures: dict[str, NotifyError] = {}
        for chat_id in self.chat_ids:
            try:
                for index, part in enumerate(parts):
                    key = f"{chat_id}:{index}"
                    if key in progress:
                        continue
                    await self.notifier.send_part(chat_id, part, bool(payload.get("silent")))
                    progress.add(key)
                    if single:
                        self.store.set_event_progress(events[0]["id"], sorted(progress))
            except NotifyError as exc:
                failures[chat_id] = exc
        ids = [event["id"] for event in events]
        if not failures:
            self.store.mark_events_sent(ids)
            if w is not None:
                self.store.update_watch(w.id, last_notified=time.time())
            return True

        attempts = max(event["attempts"] for event in events) + 1
        details = "; ".join(f"chat {chat}: {exc}" for chat, exc in failures.items())
        advice = " ".join(filter(None, {explain_telegram_error(exc) for exc in failures.values()}))
        rejected = all(isinstance(exc, TelegramApiError) and exc.code in (400, 403) for exc in failures.values())
        bad_message = all(isinstance(exc, TelegramApiError) and exc.code == 400
                          and _CONTENT_ERROR.search(exc.description) for exc in failures.values())
        delivered_somewhere = len(failures) < len(self.chat_ids)
        if (rejected and delivered_somewhere) or (bad_message and attempts >= 3):
            # Retrying a message Telegram rejects (bad chat id, invalid content) would block every
            # later notification of this watch, so close it and move on.
            self.store.finish_events(ids, "sent" if delivered_somewhere else "failed", details)
            log.error("Notification #%s could not be delivered everywhere: %s %s", ids[0], details, advice)
            if w is not None and delivered_somewhere:
                self.store.update_watch(w.id, last_notified=time.time())
            return True
        retry_after = max((exc.retry_after or 0) for exc in failures.values())
        delay = max(retry_after, min(900, 10 * 2 ** min(attempts - 1, 7)))
        for event in events:
            self.store.mark_event_retry(event["id"], attempts, time.time() + delay, details)
        log.error("Notification not delivered (attempt %d, next try in %ds): %s %s", attempts, delay, details, advice)
        return False

    # ------------------------------------------------------------------ reports & commands

    def _startup_report(self) -> tuple[str, list[str]]:
        header = (f"✅ <b>مانیتور شروع به کار کرد</b>\n🕒 {esc(format_ts(time.time(), self.tz))}\n"
                  f"👁 {len(self.watches)} مورد تحت نظر:")
        blocks = []
        for w in self.watches:
            kind = TYPE_LABELS[w.type] + (" + خزش کل بخش" if w.crawl else "")
            blocks.append(f"• <b>{esc(w.label)}</b> — {kind} · هر {human_duration(w.interval_seconds)}")
        return header, blocks

    def status_report(self, daily: bool = False) -> tuple[str, list[str]]:
        now = time.time()
        counts = self.store.event_counts_since(now - 86400)
        title = "🗓 <b>گزارش روزانهٔ مانیتور</b>" if daily else "📊 <b>وضعیت مانیتور</b>"
        header = (f"{title}\n🕒 {esc(format_ts(now, self.tz))}\n"
                  f"⏱ فعال از {human_duration(now - self.started_at)} پیش")
        blocks = []
        for w in self.config.watches:
            state = self.store.watch_state(w.id)
            events = counts.get(w.id, Counter())
            changes = sum(events[k] for k in DIGESTABLE)
            if not w.enabled:
                icon, desc = "⏸", "غیرفعال"
            elif state.get("consecutive_errors"):
                icon = "⚠️"
                desc = f"{state['consecutive_errors']} خطای پیاپی: {esc(truncate(state.get('last_error') or '', 100))}"
            elif state.get("last_ok"):
                icon, desc = "✅", f"آخرین بررسی موفق {human_duration(now - state['last_ok'])} پیش"
            else:
                icon, desc = "⏳", "هنوز بررسی نشده"
            blocks.append(f"{icon} <b>{esc(w.label)}</b> — {desc}\n"
                          f"⚙️ {self.engine_for(w)} · هر {human_duration(w.interval_seconds)} · "
                          f"رویدادهای ۲۴ ساعت اخیر: {changes}")
        pending = self.store.pending_count()
        if pending:
            blocks.append(f"📮 {pending} پیام در صف ارسال")
        return header, blocks

    async def _housekeeping_loop(self) -> None:
        next_report = self._next_report_time()
        last_prune = 0.0
        while True:
            now = time.time()
            if now - last_prune > 6 * 3600:
                try:
                    self.store.prune(self.config.history_days)
                except Exception:
                    log.exception("pruning the database failed")
                last_prune = now
            if next_report and now >= next_report:
                self._enqueue_info(*self.status_report(daily=True))
                next_report = self._next_report_time()
            await asyncio.sleep(30)

    def _next_report_time(self) -> float | None:
        hour = self.config.daily_report_hour
        if hour is None:
            return None
        now = datetime.now(self.tz)
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    async def _command_loop(self) -> None:
        """Answer /status, /check, /list and /help from the configured chats."""
        allowed = {str(chat) for chat in self.config.telegram.chat_ids}
        offset = None
        while True:
            try:
                updates = await self.notifier.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except NotifyError as exc:
                code = getattr(exc, "code", None)
                if code == 409:
                    log.warning("Telegram commands paused: another program is reading this bot's updates "
                                "(webhook or a second monitor instance)")
                    await asyncio.sleep(300)
                else:
                    log.debug("getUpdates failed: %s", exc)
                    await asyncio.sleep(20)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post") or {}
                text = (message.get("text") or "").strip()
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if not text.startswith("/") or message.get("date", 0) < self.started_at - 120:
                    continue
                if chat_id not in allowed:
                    log.warning("Ignoring command %r from unauthorised chat %s", text[:40], chat_id)
                    continue
                try:
                    await self._handle_command(chat_id, text.split()[0].split("@")[0].lower())
                except Exception:
                    log.exception("command %r failed", text[:40])

    async def _handle_command(self, chat_id: str, command: str) -> None:
        if command == "/status":
            header, blocks = self.status_report()
        elif command == "/check":
            self.trigger_all()
            header, blocks = "⏳ بررسی فوری همهٔ موارد شروع شد؛ هر تغییری بلافاصله گزارش می‌شود.", []
        elif command == "/list":
            header = f"📋 <b>موارد تحت نظر ({len(self.config.watches)})</b>"
            blocks = [f"{'•' if w.enabled else '⏸'} <b>{esc(w.label)}</b> ({esc(w.id)})\n{esc(w.url)}"
                      for w in self.config.watches]
        else:
            header = ("🤖 <b>دستورات</b>\n/status — وضعیت همهٔ موارد\n/check — بررسی فوری همه\n"
                      "/list — فهرست سایت‌های تحت نظر")
            blocks = []
        for part in build_parts(report.info_event(header, blocks)):
            await self.notifier.send_part(chat_id, part)

    # ------------------------------------------------------------------ one-shot (CLI)

    async def run_once(self, watches: list) -> list[tuple[object, str]]:
        results = []
        for w in watches:
            outcome = await self.check_watch(w)
            if w.crawl and not outcome.startswith("error"):
                outcome += " | " + await self.crawl(w)
            results.append((w, outcome))
        await self.dispatch_pending()
        return results

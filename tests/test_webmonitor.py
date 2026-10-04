"""Tests: unit tests plus end-to-end runs against a local fake website and a fake Telegram API.

Run from the project folder:
    python -m unittest discover -s tests -v
Set WEBMONITOR_SKIP_BROWSER=1 to skip the (slower) real-browser test.
"""

from __future__ import annotations

import asyncio
import html
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webmonitor import report  # noqa: E402
from webmonitor.config import ConfigError, parse_config  # noqa: E402
from webmonitor.engine import Monitor  # noqa: E402
from webmonitor.extract import (  # noqa: E402
    ExtractError, detect_block, extract_items, normalize_url, page_snapshot, parse_feed, parse_html,
)
from webmonitor.notifier import TEXT_LIMIT, build_parts, html_to_text  # noqa: E402
from webmonitor.util import clean_text, find_keywords, gregorian_to_jalali  # noqa: E402

CFG_PATH = Path(tempfile.gettempdir()) / "webmonitor-test.json"


def page_html(lines: list[str], title: str = "Test page") -> str:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    return f"<html><head><title>{title}</title></head><body><nav>menu</nav><div id='main'>{body}</div></body></html>"


def section_html(links: list[str]) -> str:
    items = "".join(f"<li><a href='{link}'>Link {link}</a></li>" for link in links)
    return f"<html><body><div id='main'><h1>Section</h1><ul>{items}</ul></div></body></html>"


def news_html(entries: list[tuple[str, str]]) -> str:
    items = "".join(f"<li><h3><a href='{href}'>{title}</a></h3><p>Summary of {title}</p></li>" for href, title in entries)
    return f"<html><body><ul class='news'>{items}</ul></body></html>"


def rss(entries: list[tuple[str, str]]) -> bytes:
    items = "".join(f"<item><title>{t}</title><link>https://n.example/{g}</link><guid>{g}</guid>"
                    f"<description>About {t}</description></item>" for g, t in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>C</title>{items}</channel></rss>'.encode()


# =========================================================================== unit tests

class UtilTests(unittest.TestCase):
    def test_jalali_conversion(self):
        self.assertEqual(gregorian_to_jalali(2026, 10, 4), (1405, 7, 12))
        self.assertEqual(gregorian_to_jalali(2026, 3, 21), (1405, 1, 1))
        self.assertEqual(gregorian_to_jalali(2026, 3, 20), (1404, 12, 29))
        self.assertEqual(gregorian_to_jalali(2025, 3, 21), (1404, 1, 1))
        self.assertEqual(gregorian_to_jalali(2024, 3, 20), (1403, 1, 1))

    def test_clean_text_keeps_zwnj(self):
        self.assertEqual(clean_text("  می‌خواهم ​\n\tویزا‏  "), "می‌خواهم ویزا")

    def test_keywords_normalisation(self):
        self.assertEqual(find_keywords(["نوبت", "visa"], "نوبتدهي VISA"), ["نوبت", "visa"])
        self.assertEqual(find_keywords(["کیک"], "كيك"), ["کیک"])
        self.assertEqual(find_keywords(["1405"], "سال ۱۴۰۵"), ["1405"])
        self.assertEqual(find_keywords(["termin"], "nothing here"), [])


PAGE = """<html><head><title>T</title><script>var secret = 1;</script><style>.a{}</style></head><body>
<nav><a href="/menu">Menu</a></nav>
<div id="main">
  <h1>Visa   information</h1>
  <p>First <b>bold</b> paragraph.<br>Second line</p>
  <div class="accordion" style="display:none"><p>Hidden detail</p></div>
  <table><tr><th>Type</th><th>Fee</th></tr><tr><td>Schengen</td><td>90 EUR</td></tr></table>
  <ul><li><a href="docs/form.pdf?utm_source=x#top">Form</a></li><li><a href="https://other.example/x">Other</a></li></ul>
  <div class="ads">Buy now</div>
  <pre>line one
line two</pre>
</div>
<div class="cc-window">We use cookies</div>
</body></html>"""
BASE = "https://site.example/visa/index.html"


class ExtractTests(unittest.TestCase):
    def snap(self, **kwargs):
        return page_snapshot(parse_html(PAGE, BASE), BASE, **kwargs)

    def test_lines_blocks_tables_hidden_and_excludes(self):
        snap = self.snap(selector="#main", exclude_selectors=[".ads"])
        self.assertEqual(snap.lines, ["Visa information", "First bold paragraph.", "Second line", "Hidden detail",
                                      "Type | Fee", "Schengen | 90 EUR", "Form", "Other", "line one", "line two"])
        self.assertEqual(snap.title, "T")

    def test_links_are_absolute_and_clean(self):
        snap = self.snap(selector="#main", track_links=True)
        self.assertIn(("Form", "https://site.example/visa/docs/form.pdf"), snap.links)
        self.assertIn(("Other", "https://other.example/x"), snap.links)

    def test_missing_selector_raises(self):
        with self.assertRaises(ExtractError) as ctx:
            self.snap(selector="#nope")
        self.assertEqual(ctx.exception.kind, "selector")

    def test_ignore_patterns(self):
        snap = self.snap(selector="#main", ignore_patterns=[re.compile(r"\d+ EUR")])
        self.assertIn("Schengen", snap.lines)

    def test_whole_body_skips_scripts_and_cookie_banners(self):
        text = " ".join(self.snap().lines)
        self.assertIn("Menu", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("cookies", text)

    def test_nested_matches_are_not_duplicated(self):
        doc = parse_html("<div class='c'>A<div class='c'>B</div></div>", "https://x.example/")
        self.assertEqual(page_snapshot(doc, "https://x.example/", selector=".c").lines, ["A", "B"])

    def test_normalize_url(self):
        self.assertEqual(normalize_url("HTTPS://Site.Example:443/a b/ص?utm_medium=x&id=3#frag"),
                         "https://site.example/a%20b/%D8%B5?id=3")
        self.assertIsNone(normalize_url("mailto:someone@example.com"))

    def test_items(self):
        doc = parse_html(news_html([("/n/1", "First headline here"), ("/n/2", "Second headline here")])
                         + "<ul class='news'><li><p>An item without any link at all</p></li></ul>", "https://n.example/list")
        items = extract_items(doc, "https://n.example/list", "ul.news > li")
        self.assertEqual([i.id for i in items[:2]], ["https://n.example/n/1", "https://n.example/n/2"])
        self.assertEqual(items[0].title, "First headline here")
        self.assertEqual(items[0].text, "Summary of First headline here")
        self.assertTrue(items[2].id.startswith("h:"))

    def test_rss_and_atom(self):
        feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>C</title>'
                b'<item><title>A &amp; B</title><link>https://n.example/a</link><guid>id-a</guid>'
                b'<description><![CDATA[<p>Hello <b>world</b></p>]]></description>'
                b'<pubDate>Sun, 04 Oct 2026 13:41:33 GMT</pubDate></item>'
                b'<item><title>Second</title><link>/b</link><description>plain</description></item>'
                b'</channel></rss>')
        items = parse_feed(feed, "https://n.example/feed")
        self.assertEqual((items[0].id, items[0].title, items[0].text), ("id-a", "A & B", "Hello world"))
        self.assertEqual(items[1].url, "https://n.example/b")
        atom = (b'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>F</title>'
                b'<entry><title>Atom one</title><link rel="alternate" href="https://a.example/1"/><id>tag:a,1</id>'
                b'<summary>S1</summary><updated>2026-10-04T10:00:00Z</updated></entry></feed>')
        entry = parse_feed(atom, "https://a.example/")[0]
        self.assertEqual((entry.id, entry.url, entry.text), ("tag:a,1", "https://a.example/1", "S1"))
        with self.assertRaises(ExtractError):
            parse_feed(b"<html><body>not a feed</body></html>", "https://a.example/")

    def test_detect_block(self):
        challenge = ("<html><head><title>Just a moment...</title></head><body><div id='cf-browser-verification'>"
                     "</div><script src='/cdn-cgi/challenge-platform/x.js'></script></body></html>")
        self.assertEqual(detect_block(challenge), "Cloudflare")
        normal = ("<html><body>" + "<p>Real content paragraph here.</p>" * 200
                  + "<div class='g-recaptcha'></div></body></html>")
        self.assertIsNone(detect_block(normal))


class ReportTests(unittest.TestCase):
    def test_diff_kinds_and_context(self):
        old = ["Title", "Appointments from 15 October", "Fee 80 EUR", "Old notice"]
        new = ["Title", "Appointments from 20 October", "Fee 80 EUR", "New announcement"]
        hunks = report.diff_lines(old, new)
        self.assertEqual([h.kind for h in hunks], ["modify", "remove", "add"])
        self.assertEqual(hunks[0].context, "Title")
        self.assertEqual(hunks[1].context, "Fee 80 EUR")

    def test_moves_are_ignored(self):
        self.assertEqual(report.diff_lines(["a", "b", "c"], ["c", "a", "b"]), [])
        self.assertEqual(len(report.diff_lines(["a", "b", "c"], ["c", "a", "b"], ignore_reorder=False)), 2)

    def test_word_diff_escapes_html(self):
        out = report.word_diff_html("Fee <b> & 10 EUR", "Fee <b> & 20 EUR")
        self.assertIn("&lt;b&gt; &amp;", out)
        self.assertIn("<s>10</s> <b><u>20</u></b>", out)

    def test_long_reports_are_split_with_valid_html(self):
        blocks = [f"🟢 <b>line {i}</b> " + "x" * 300 for i in range(60)]
        payload = report._payload("<b>Header</b>", "<i>continued</i>", blocks, silent=False, doc_title="t")
        parts = build_parts(payload)
        texts = [content for kind, content in parts if kind == "text"]
        self.assertLessEqual(len(texts), 5)
        self.assertTrue(all(len(t) <= TEXT_LIMIT for t in texts))
        self.assertTrue(all(t.count("<b>") == t.count("</b>") for t in texts))
        self.assertTrue(texts[1].startswith("<i>continued</i>"))
        self.assertEqual(parts[-1][0], "doc")
        self.assertIn("line 59", parts[-1][1]["content"])

    def test_short_reports_have_no_attachment(self):
        payload = report._payload("<b>H</b>", "", ["a", "b"], silent=False, doc_title="t")
        self.assertEqual([kind for kind, _ in build_parts(payload)], ["text"])

    def test_html_to_text(self):
        self.assertEqual(
            html_to_text('A <s>x</s> <b><u>y</u></b> <a href="https://e.x/?a=1&amp;b=2">L</a> &lt;'),
            "A [-x-] {+y+} L (https://e.x/?a=1&b=2) <")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("TELEGRAM_CHAT_ID", None)

    def test_old_flat_format_still_works(self):
        raw = {"telegram_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", "telegram_chat_id": "42",
               "max_concurrent_tasks": 3, "global_proxy": None,
               "sites": [{"id": "s1", "url": "https://e.example/p", "selector": ".content-section",
                          "check_interval_seconds": 300, "jitter_seconds": 45,
                          "notification_cooldown_minutes": 30, "persist_session": False, "proxy": None,
                          "min_content_length": 100}]}
        cfg = parse_config(raw, CFG_PATH)
        w = cfg.watches[0]
        self.assertEqual((w.interval_seconds, w.cooldown_minutes, w.min_content_length), (300, 30, 100))
        self.assertEqual((cfg.telegram.chat_ids, cfg.telegram.enabled, cfg.browser.max_pages), (["42"], True, 3))
        self.assertEqual(cfg.warnings, [])

    def test_placeholders_disable_telegram(self):
        cfg = parse_config({"telegram": {"token": "YOUR_TELEGRAM_BOT_TOKEN", "chat_ids": ["YOUR_CHAT_ID"]},
                            "watches": [{"id": "a", "url": "https://e.example"}]}, CFG_PATH)
        self.assertFalse(cfg.telegram.enabled)

    def test_environment_overrides_token(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "999:ZYXWVUTSRQPONMLKJIHGFEDCBA"
        cfg = parse_config({"telegram": {"chat_ids": [1]}, "watches": [{"id": "a", "url": "https://e.example"}]},
                           CFG_PATH)
        self.assertEqual((cfg.telegram.token, cfg.telegram.chat_ids), ("999:ZYXWVUTSRQPONMLKJIHGFEDCBA", ["1"]))

    def test_invalid_configs_are_rejected(self):
        ok = {"id": "a", "url": "https://e.example"}
        bad = [
            {"watches": [{"id": "a", "url": "ftp://x"}]},
            {"watches": [{"id": "a b", "url": "https://e.example"}]},
            {"watches": [{**ok, "selector": "div[["}]},
            {"watches": [{**ok, "ignore_patterns": ["("]}]},
            {"watches": [ok, ok]},
            {"watches": [{**ok, "type": "items"}]},
            {"watches": [{**ok, "interval_seconds": 1}]},
            {"watches": [{**ok, "track_links": "yes"}]},
            {"watches": []},
            {"telegram": {"token": "not-a-token"}, "watches": [ok]},
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                parse_config(raw, CFG_PATH)

    def test_unknown_keys_warn(self):
        cfg = parse_config({"watches": [{"id": "a", "url": "https://e.example", "selecter": "#x"}]}, CFG_PATH)
        self.assertTrue(any("selecter" in warning for warning in cfg.warnings))


# =========================================================================== end-to-end

class FakeSite:
    def __init__(self):
        self.routes: dict[str, dict] = {}
        self.hits: Counter = Counter()

    def set(self, path: str, body, status: int = 200, ctype: str = "text/html; charset=utf-8", etag=None):
        self.routes[path] = {"body": body.encode() if isinstance(body, str) else body, "status": status,
                             "ctype": ctype, "etag": etag}

    def remove(self, path: str) -> None:
        self.routes.pop(path, None)

    async def handle(self, request: web.Request) -> web.Response:
        self.hits[request.path] += 1
        route = self.routes.get(request.path)
        if route is None:
            return web.Response(status=404, text="not found")
        headers = {"Content-Type": route["ctype"]}
        if route["etag"]:
            headers["ETag"] = route["etag"]
            if request.headers.get("If-None-Match") == route["etag"]:
                return web.Response(status=304, headers={"ETag": route["etag"]})
        return web.Response(status=route["status"], body=route["body"], headers=headers)


class FakeTelegram:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.failures: list[tuple[int, dict]] = []
        self.bad_chats: set[str] = set()

    async def handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        if request.content_type.startswith("multipart"):
            form = await request.post()
            data = {k: (v if isinstance(v, str) else v.file.read()) for k, v in form.items()}
        else:
            data = await request.json()
        if method.startswith("send") and str(data.get("chat_id")) in self.bad_chats:
            return web.json_response({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                                     status=400)
        if method.startswith("send") and self.failures:
            status, body = self.failures.pop(0)
            return web.json_response(body, status=status)
        self.calls.append((method, data))
        return web.json_response({"ok": True, "result": [] if method == "getUpdates" else {"message_id": 1}})

    @property
    def messages(self) -> list[dict]:
        return [data for method, data in self.calls if method == "sendMessage"]

    @property
    def documents(self) -> list[dict]:
        return [data for method, data in self.calls if method == "sendDocument"]


async def serve(handler, route: str = "/{tail:.*}"):
    app = web.Application()
    app.router.add_route("*", route, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


class EngineTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="webmonitor-test-"))
        self.site, self.tg = FakeSite(), FakeTelegram()
        self.site_runner, self.site_port = await serve(self.site.handle)
        self.tg_runner, self.tg_port = await serve(self.tg.handle, "/bot{token}/{method}")
        self.monitors: list[Monitor] = []

    async def asyncTearDown(self):
        for monitor in self.monitors:
            await monitor.close()
        await self.site_runner.cleanup()
        await self.tg_runner.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.site_port}{path}"

    async def monitor(self, watches: list[dict], **top) -> Monitor:
        raw = {
            "data_dir": str(self.tmp / "data"), "notify_startup": False, "daily_report_hour": None,
            "telegram": {"token": "123456:" + "A" * 35, "chat_ids": ["42"],
                         "api_base": f"http://127.0.0.1:{self.tg_port}", "commands": False},
            "defaults": {"engine": "http", "timeout_seconds": 10, "min_content_length": 1},
            "watches": watches, **top,
        }
        monitor = Monitor(parse_config(raw, self.tmp / "cfg.json"))
        monitor.notifier.retry_delay = 0.01
        await monitor.start()
        self.monitors.append(monitor)
        return monitor

    def page_watch(self, **extra) -> dict:
        return {"id": "p", "name": "Embassy", "url": self.url("/page"), "selector": "#main", **extra}


class PageFlowTests(EngineTestCase):
    async def test_change_is_reported_with_word_diff_and_escaping(self):
        self.site.set("/page", page_html(["Appointments from 15 October", "Fee: 80 EUR"]))
        m = await self.monitor([self.page_watch(keywords=["appointment"])])
        w = m.watches[0]
        self.assertTrue((await m.check_watch(w)).startswith("baseline"))
        self.assertEqual(await m.check_watch(w), "no change")
        self.site.set("/page", page_html(["Appointments from 20 October", "Fee: 80 EUR",
                                          "New rule: <b>bring</b> passport & photo"]))
        self.assertTrue((await m.check_watch(w)).startswith("CHANGE"))
        self.assertEqual(await m.dispatch_pending(), 1)
        message = self.tg.messages[-1]
        self.assertEqual((message["parse_mode"], message["chat_id"]), ("HTML", "42"))
        self.assertIn("<s>15</s> <b><u>20</u></b>", message["text"])
        self.assertIn("New rule: &lt;b&gt;bring&lt;/b&gt; passport &amp; photo", message["text"])
        self.assertTrue(message["text"].startswith("🚨"))
        self.assertFalse(message["disable_notification"])

    async def test_nothing_is_lost_while_telegram_is_down(self):
        self.site.set("/page", page_html(["version one"]))
        m = await self.monitor([self.page_watch()])
        await m.check_watch(m.watches[0])
        self.site.set("/page", page_html(["version two"]))
        await m.check_watch(m.watches[0])
        self.tg.failures = [(500, {"ok": False, "error_code": 500, "description": "Internal"})] * 3
        self.assertEqual(await m.dispatch_pending(), 0)
        self.assertEqual(m.store.pending_count(), 1)
        await m.close()  # simulate a restart: the queued notification must survive
        self.monitors.remove(m)
        m2 = await self.monitor([self.page_watch()])
        with m2.store.conn:
            m2.store.conn.execute("UPDATE events SET next_attempt = 0")
        self.assertEqual(await m2.dispatch_pending(), 1)
        self.assertIn("two", self.tg.messages[-1]["text"])
        self.assertEqual(m2.store.pending_count(), 0)

    async def test_rate_limit_and_html_rejection_fallback(self):
        self.site.set("/page", page_html(["alpha beta"]))
        m = await self.monitor([self.page_watch()])
        await m.check_watch(m.watches[0])
        self.site.set("/page", page_html(["alpha gamma"]))
        await m.check_watch(m.watches[0])
        self.tg.failures = [
            (429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 1}}),
            (400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities"}),
        ]
        self.assertEqual(await m.dispatch_pending(), 1)
        last = self.tg.messages[-1]
        self.assertNotIn("parse_mode", last)
        self.assertIn("[-beta-] {+gamma+}", last["text"])

    async def test_one_wrong_chat_id_does_not_block_the_others(self):
        self.site.set("/page", page_html(["first"]))
        m = await self.monitor([self.page_watch()])
        m.chat_ids = ["42", "999"]
        self.tg.bad_chats = {"999"}
        w = m.watches[0]
        await m.check_watch(w)
        for text in ("second", "third"):
            self.site.set("/page", page_html([text]))
            await m.check_watch(w)
        self.assertEqual(await m.dispatch_pending(), 2)
        self.assertEqual([msg["chat_id"] for msg in self.tg.messages], ["42", "42"])
        self.assertEqual(m.store.pending_count(), 0)

    async def test_errors_alert_once_then_recovery_without_false_change(self):
        self.site.set("/page", page_html(["stable text"]))
        m = await self.monitor([self.page_watch(error_threshold=2)])
        w = m.watches[0]
        await m.check_watch(w)
        self.site.set("/page", "<html><body>Service Unavailable</body></html>", status=503)
        for _ in range(4):
            self.assertTrue((await m.check_watch(w)).startswith("error"))
        self.site.set("/page", page_html(["stable text"]))
        self.assertEqual(await m.check_watch(w), "no change")  # the error page never replaced the baseline
        await m.dispatch_pending()
        texts = [message["text"] for message in self.tg.messages]
        self.assertEqual(sum("مشکل در بررسی" in t for t in texts), 1)
        self.assertEqual(sum("دوباره با موفقیت" in t for t in texts), 1)

    async def test_broken_selector_is_an_error_not_a_change(self):
        self.site.set("/page", page_html(["content here"]))
        m = await self.monitor([self.page_watch()])
        await m.check_watch(m.watches[0])
        self.site.set("/page", "<html><body><div id='redesigned'>new layout</div></body></html>")
        self.assertIn("matched nothing", await m.check_watch(m.watches[0]))
        self.assertEqual(m.store.pending_count(), 0)

    async def test_cooldown_batches_changes_without_losing_any(self):
        self.site.set("/page", page_html(["version v0"]))
        m = await self.monitor([self.page_watch(cooldown_minutes=10)])
        w = m.watches[0]
        await m.check_watch(w)
        for version in ("v1", "v2", "v3"):
            self.site.set("/page", page_html([f"version {version}"]))
            await m.check_watch(w)
            await m.dispatch_pending()
        self.assertEqual(len(self.tg.messages), 1)  # v1 sent at once, v2+v3 wait for the cooldown
        self.assertEqual(m.store.pending_count(), 2)
        m.store.update_watch(w.id, last_notified=time.time() - 11 * 60)
        self.assertEqual(await m.dispatch_pending(), 2)
        digest = self.tg.messages[-1]["text"]
        self.assertIn("2 رویداد", digest)
        self.assertIn("v2", digest)
        self.assertIn("v3", digest)

    async def test_conditional_get_and_silent_rebaseline_on_settings_change(self):
        self.site.set("/page", page_html(["etag text"]), etag='"v1"')
        m = await self.monitor([self.page_watch()])
        await m.check_watch(m.watches[0])
        self.assertEqual(await m.check_watch(m.watches[0]), "no change (HTTP 304)")
        await m.close()
        self.monitors.remove(m)
        m2 = await self.monitor([self.page_watch(selector="body")])
        self.assertIn("settings changed", await m2.check_watch(m2.watches[0]))
        self.assertEqual(m2.store.pending_count(), 0)

    async def test_only_keywords_filters_but_keeps_history(self):
        self.site.set("/page", page_html(["nothing important"]))
        m = await self.monitor([self.page_watch(keywords=["visa"], only_keywords=True)])
        w = m.watches[0]
        await m.check_watch(w)
        self.site.set("/page", page_html(["still nothing important"]))
        self.assertIn("not sent", await m.check_watch(w))
        self.site.set("/page", page_html(["VISA appointments open"]))
        await m.check_watch(w)
        self.assertEqual(await m.dispatch_pending(), 1)
        self.assertIn("VISA appointments", self.tg.messages[-1]["text"])
        statuses = [row["status"] for row in m.store.recent_events("p")]
        self.assertEqual(sorted(statuses), ["filtered", "sent"])

    async def test_status_command_and_run_forever_lifecycle(self):
        self.site.set("/page", page_html(["hello world"]))
        m = await self.monitor([self.page_watch(interval_seconds=5)], notify_startup=True)
        task = asyncio.create_task(m.run_forever())
        for _ in range(150):
            await asyncio.sleep(0.1)
            if m.store.get_page("p", m._root(m.watches[0])) is not None and self.tg.messages:
                break
        m.stop()
        await asyncio.wait_for(task, 20)
        self.assertIn("شروع به کار", self.tg.messages[0]["text"])
        await m._handle_command("42", "/status")
        self.assertIn("وضعیت مانیتور", self.tg.messages[-1]["text"])
        self.assertIn("Embassy", self.tg.messages[-1]["text"])


class ItemsAndFeedTests(EngineTestCase):
    async def test_new_list_items_are_reported(self):
        self.site.set("/news", news_html([("/n/1", "First headline here")]))
        m = await self.monitor([{"id": "n", "type": "items", "url": self.url("/news"), "item_selector": "ul.news > li"}])
        w = m.watches[0]
        self.assertIn("baseline", await m.check_watch(w))
        self.site.set("/news", news_html([("/n/2", "Brand new headline"), ("/n/1", "First headline here")]))
        self.assertIn("NEW", await m.check_watch(w))
        await m.dispatch_pending()
        text = self.tg.messages[-1]["text"]
        self.assertIn("Brand new headline", text)
        self.assertNotIn("First headline", text)
        self.assertIn(self.url("/n/2"), text)

    async def test_new_feed_entries_are_reported(self):
        self.site.set("/feed.xml", rss([("a", "Old story")]), ctype="application/rss+xml")
        m = await self.monitor([{"id": "f", "type": "feed", "url": self.url("/feed.xml")}])
        w = m.watches[0]
        self.assertIn("baseline", await m.check_watch(w))
        self.site.set("/feed.xml", rss([("b", "Breaking story"), ("a", "Old story")]), ctype="application/rss+xml")
        self.assertIn("NEW", await m.check_watch(w))
        await m.dispatch_pending()
        self.assertIn("Breaking story", self.tg.messages[-1]["text"])


class CrawlTests(EngineTestCase):
    async def test_new_changed_removed_pages_and_files(self):
        s = self.site
        s.set("/sec/", section_html(["/sec/a", "/sec/b", "/sec/doc.pdf", "/other/x"]))
        s.set("/sec/a", page_html(["Page A"]))
        s.set("/sec/b", page_html(["Page B"]))
        s.set("/sec/doc.pdf", b"%PDF-1.4 version 1", ctype="application/pdf")
        s.set("/other/x", page_html(["out of scope"]))
        m = await self.monitor([{"id": "c", "url": self.url("/sec/"), "selector": "#main",
                                 "crawl": {"max_pages": 10, "max_depth": 2, "interval_seconds": 60}}])
        w = m.watches[0]
        await m.check_watch(w)
        self.assertIn("3 checked", await m.crawl(w))
        self.assertEqual(m.store.pending_count(), 0)  # the first crawl is a silent baseline
        self.assertEqual(s.hits["/other/x"], 0)

        s.set("/sec/c", page_html(["Brand new visa page"], title="New page"))
        s.set("/sec/", section_html(["/sec/a", "/sec/b", "/sec/doc.pdf", "/sec/c"]))
        await m.check_watch(w)  # the start page shows a new link -> queued for an immediate check
        self.assertIn(self.url("/sec/c"), m._crawl_pending[w.id])
        await m._crawl_new_urls(w, sorted(m._crawl_pending[w.id]))

        s.set("/sec/a", page_html(["Page A updated"]))
        s.set("/sec/doc.pdf", b"%PDF-1.4 version 2", ctype="application/pdf")
        s.remove("/sec/b")
        await m.crawl(w)
        await m.crawl(w)  # removal is confirmed after two consecutive 404s
        kinds = Counter(row["kind"] for row in m.store.conn.execute("SELECT kind FROM events"))
        self.assertEqual(kinds, Counter({"change": 2, "new_page": 1, "file": 1, "removed_page": 1}))
        await m.dispatch_pending()
        texts = "\n".join(message["text"] for message in self.tg.messages)
        self.assertIn("Brand new visa page", texts)
        self.assertIn("doc.pdf", texts)
        self.assertIn("حذف شد", texts)


class BrowserTests(EngineTestCase):
    async def test_auto_engine_switches_to_browser_for_javascript_pages(self):
        if os.environ.get("WEBMONITOR_SKIP_BROWSER"):
            self.skipTest("browser tests disabled")
        self.site.set("/app", "<html><body><div id='main'></div><noscript>Please enable JavaScript</noscript>"
                              "<script>document.getElementById('main').innerHTML ="
                              " '<p>Rendered by JavaScript: appointments open</p>';</script></body></html>")
        m = await self.monitor([{"id": "js", "url": self.url("/app"), "selector": "#main", "engine": "auto",
                                 "min_content_length": 10}])
        w = m.watches[0]
        result = await m.check_watch(w)
        if m.browser.unavailable:
            self.skipTest(f"no browser available: {m.browser.unavailable}")
        self.assertTrue(result.startswith("baseline"), result)
        self.assertEqual(m.engine_for(w), "browser")
        self.assertIn("Rendered by JavaScript", m.store.get_page("js", m._root(w))["snapshot"])
        self.assertEqual(await m.check_watch(w), "no change")


if __name__ == "__main__":
    unittest.main()

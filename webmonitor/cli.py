"""Command-line interface: python monitor_system.py <command> [options]"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config, parse_config
from .engine import Monitor
from .notifier import NotifyError, TelegramNotifier, build_parts, explain_telegram_error, html_to_text
from .storage import Store
from .util import format_ts, load_timezone, truncate

log = logging.getLogger("webmonitor")
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "monitor-config.json"


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def setup_logging(data_dir: Path, verbose: bool, to_file: bool) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    under_systemd = bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(
        "[%(levelname)s] %(message)s" if under_systemd else "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if to_file and not under_systemd:
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(data_dir / "monitor.log", maxBytes=5 * 1024 * 1024, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(handler)
    for noisy in ("asyncio", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _select(config, ids: list[str]) -> list:
    if not ids:
        return [w for w in config.watches if w.enabled]
    selected = []
    for watch_id in ids:
        watch = config.watch(watch_id)
        if watch is None:
            raise ConfigError(f"no watch with id {watch_id!r} (known: {', '.join(w.id for w in config.watches)})")
        selected.append(watch)
    return selected


# --------------------------------------------------------------------------- commands

async def cmd_run(config, args) -> int:
    if not config.telegram.enabled:
        log.warning("Telegram is not configured — notifications are only printed here. "
                    "Set telegram.token and telegram.chat_ids in %s", config.path.name)
    monitor = Monitor(config)
    await monitor.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, monitor.stop)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows: Ctrl+C raises KeyboardInterrupt instead
    try:
        await monitor.run_forever()
    finally:
        await monitor.close()
    return 0


async def cmd_once(config, args) -> int:
    watches = _select(config, args.watch)
    monitor = Monitor(config, dry_run=args.dry_run)
    await monitor.start()
    try:
        results = await monitor.run_once(watches)
    finally:
        await monitor.close()
    print("\n=== Results ===")
    failed = 0
    for watch, outcome in results:
        failed += outcome.startswith("error")
        print(f"{watch.id:<24} {outcome}")
    return 1 if failed else 0


async def cmd_preview(config, args) -> int:
    watch = _select(config, [args.watch])[0]
    monitor = Monitor(config, dry_run=True)
    await monitor.start()
    try:
        if watch.type == "page":
            result, parsed = await monitor._fetch_parse(watch, watch.url, lambda r: monitor._parse_page(watch, r),
                                                        wait_for=watch.selector, allow_switch=False)
            snap, discovered = parsed
            print(f"engine={result.engine}  HTTP {result.status}  {result.elapsed:.1f}s  title: {snap.title}")
            print(f"=== {len(snap.lines)} lines, {snap.text_length:,} chars ===")
            for line in snap.lines[: args.limit]:
                print(f"  {line}")
            if len(snap.lines) > args.limit:
                print(f"  … {len(snap.lines) - args.limit} more lines (use --limit)")
            if snap.links:
                print(f"=== {len(snap.links)} tracked links ===")
                for text, url in snap.links[: args.limit]:
                    print(f"  {truncate(text, 70):<70}  {url}")
            if watch.crawl:
                scoped = [u for _, u in discovered if monitor._crawl_kind(watch, u)]
                print(f"=== crawl scope: {len(scoped)} in-scope link(s) on the start page ===")
                for url in scoped[: args.limit]:
                    print(f"  {url}")
        else:
            parse = monitor._parse_items if watch.type == "items" else monitor._parse_feed
            result, items = await monitor._fetch_parse(watch, watch.url, lambda r: parse(watch, r),
                                                       wait_for=watch.wait_selector, allow_switch=False)
            print(f"engine={result.engine}  HTTP {result.status}  {result.elapsed:.1f}s  — {len(items)} items")
            for item in items[: args.limit]:
                print(f" • {truncate(item.title, 140)}\n   {item.url or '(no link)'}  [id={truncate(item.id, 60)}]")
    except Exception as exc:  # FetchError / ExtractError
        print(f"FAILED: {exc}")
        return 1
    finally:
        await monitor.close()
    return 0


async def cmd_inspect(config, args) -> int:
    from .analyzer import inspect
    return await inspect(config, args.url, use_browser=args.browser, selector=args.selector or "",
                         items=args.items or "")


async def cmd_test_telegram(config, args) -> int:
    if not config.telegram.token:
        print("No bot token configured (telegram.token or the TELEGRAM_BOT_TOKEN environment variable).")
        return 1
    notifier = TelegramNotifier(config.telegram)
    try:
        me = await notifier.call("getMe")
        print(f"Bot OK: @{me.get('username')} ({me.get('first_name')})")
        if not config.telegram.chat_ids:
            print("No chat_ids configured yet. Run:  python monitor_system.py chat-id")
            return 1
        for chat_id in config.telegram.chat_ids:
            await notifier.send_text(chat_id, "✅ <b>تست موفق</b>\nربات مانیتور می‌تواند به این چت پیام بفرستد.")
            print(f"Test message sent to {chat_id}")
    except NotifyError as exc:
        advice = explain_telegram_error(exc)
        print(f"FAILED: {exc}" + (f"\n→ {advice}" if advice else ""))
        return 1
    finally:
        await notifier.close()
    return 0


async def cmd_chat_id(config, args) -> int:
    if not config.telegram.token:
        print("Set telegram.token first (from @BotFather).")
        return 1
    notifier = TelegramNotifier(config.telegram)
    try:
        updates = await notifier.call("getUpdates", {"timeout": 0,
                                                     "allowed_updates": ["message", "channel_post", "my_chat_member"]})
    except NotifyError as exc:
        print(f"FAILED: {exc}\n(If the monitor service is running it consumes the updates — stop it first.)")
        return 1
    finally:
        await notifier.close()
    chats = {}
    for update in updates or []:
        holder = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = holder.get("chat")
        if chat:
            chats[chat["id"]] = chat
    if not chats:
        print("No chats found. Open your bot in Telegram and press Start (or send it any message).\n"
              "For a group/channel: add the bot (as admin for channels), post a message, then run this again.")
        return 1
    for chat in chats.values():
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        print(f"chat_id: {chat['id']:<16} type: {chat.get('type', ''):<10} name: {name}")
    print('\nPut the id(s) into "chat_ids" in monitor-config.json, e.g.  "chat_ids": ["123456789"]')
    return 0


async def cmd_history(config, args) -> int:
    store = Store(config.data_dir / "monitor.db")
    tz = load_timezone(config.timezone)
    try:
        if args.show:
            row = store.conn.execute("SELECT * FROM events WHERE id = ?", (args.show,)).fetchone()
            if row is None:
                print(f"No event #{args.show}")
                return 1
            for kind, content in build_parts(json.loads(row["payload"])):
                if kind == "text":
                    print(html_to_text(content))
                    print("─" * 60)
            return 0
        rows = store.recent_events(args.watch, args.limit)
        if not rows:
            print("No events recorded yet.")
        for row in reversed(rows):
            print(f"#{row['id']:<5} {format_ts(row['created'], tz)}  {row['watch_id']:<20} {row['kind']:<12} "
                  f"{row['status']:<8} {row['summary'] or ''}")
        if rows:
            print("\nShow a full report with:  python monitor_system.py history --show <#id>")
    finally:
        store.close()
    return 0


async def cmd_reset(config, args) -> int:
    watch = _select(config, [args.watch])[0]
    store = Store(config.data_dir / "monitor.db")
    try:
        store.reset_watch(watch.id)
    finally:
        store.close()
    session = config.data_dir / "sessions" / f"{watch.id}.json"
    if session.exists():
        session.unlink()
    print(f"State of {watch.id!r} cleared; the next check saves a fresh baseline (no notification).")
    return 0


async def cmd_validate(config, args) -> int:
    print(f"Config OK: {config.path}")
    tg = config.telegram
    if tg.enabled:
        print(f"Telegram: token set, chat_ids: {', '.join(tg.chat_ids)}")
    else:
        print("Telegram: NOT configured — notifications will only be printed to the console")
    for w in config.watches:
        extra = []
        if w.crawl:
            extra.append(f"crawl≤{w.crawl.max_pages} pages/depth {w.crawl.max_depth}")
        if w.keywords:
            extra.append(f"{len(w.keywords)} keywords")
        if w.cooldown_minutes:
            extra.append(f"cooldown {w.cooldown_minutes:g}m")
        state = "on " if w.enabled else "off"
        print(f"  [{state}] {w.id:<22} {w.type:<5} {w.engine:<7} every {w.interval_seconds:g}s  {w.url}"
              + (f"  ({', '.join(extra)})" if extra else ""))
    for warning in config.warnings:
        print(f"WARNING: {warning}")
    return 0


# --------------------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="monitor_system.py",
        description="Website change monitor with Telegram alerts (embassy pages, news lists, RSS).")
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG), help="config file (default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.add_parser("run", help="start monitoring (default)")
    p = sub.add_parser("once", help="check every watch once and send/print the results")
    p.add_argument("watch", nargs="*", help="watch ids (default: all enabled)")
    p.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them")
    p = sub.add_parser("inspect", help="analyse a URL and suggest selectors / settings")
    p.add_argument("url")
    p.add_argument("--browser", action="store_true", help="load the page with the browser engine")
    p.add_argument("--selector", help="show exactly the text this selector would monitor")
    p.add_argument("--items", metavar="ITEM_SELECTOR", help="show the items this item_selector would find")
    p = sub.add_parser("preview", help="show exactly what a configured watch monitors")
    p.add_argument("watch")
    p.add_argument("--limit", type=int, default=60)
    sub.add_parser("validate", help="check the config file")
    sub.add_parser("test-telegram", help="send a test message to every chat id")
    sub.add_parser("chat-id", help="find your Telegram chat id (message your bot first)")
    p = sub.add_parser("history", help="list recent detected changes / alerts")
    p.add_argument("--watch")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--show", type=int, metavar="ID", help="print the full report of one event")
    p = sub.add_parser("reset", help="forget the stored state of a watch (fresh baseline)")
    p.add_argument("watch")
    return parser


COMMANDS = {
    "run": cmd_run, "once": cmd_once, "inspect": cmd_inspect, "preview": cmd_preview, "validate": cmd_validate,
    "test-telegram": cmd_test_telegram, "chat-id": cmd_chat_id, "history": cmd_history, "reset": cmd_reset,
}


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    command = args.command or "run"
    try:
        try:
            config = load_config(args.config)
        except ConfigError:
            if command != "inspect":
                raise
            # inspect works without a config file
            config = parse_config({"watches": [{"id": "inspect", "url": args.url}]}, Path(args.config).resolve())
        setup_logging(config.data_dir, args.verbose, to_file=command == "run")
        for warning in config.warnings:
            log.warning("config: %s", warning)
        return asyncio.run(COMMANDS[command](config, args)) or 0
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

"""Configuration loading and validation (monitor-config.json).

The old flat format (``telegram_token``, ``sites``, ``check_interval_seconds`` ...)
is still accepted so existing config files keep working.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from cssselect import SelectorError
from lxml.cssselect import CSSSelector

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9,fa;q=0.8"
PLACEHOLDER_TOKENS = {"", "YOUR_TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN"}
PLACEHOLDER_CHATS = {"", "YOUR_CHAT_ID"}
WATCH_TYPES = ("page", "items", "feed")
ENGINES = ("auto", "http", "browser")
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ConfigError(Exception):
    """Invalid configuration; the message is shown to the user as-is."""


@dataclass
class Proxy:
    server: str
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, value: Any, where: str) -> Proxy | None:
        if value in (None, "", {}):
            return None
        try:
            if isinstance(value, str):
                parts = urlsplit(value.strip())
                if not parts.scheme or not parts.hostname:
                    raise ValueError
                server = f"{parts.scheme.lower()}://{parts.hostname}"
                if parts.port:
                    server += f":{parts.port}"
                return cls(
                    server,
                    unquote(parts.username) if parts.username else None,
                    unquote(parts.password) if parts.password else None,
                )
            if isinstance(value, dict) and value.get("server"):
                server = str(value["server"]).strip()
                if "://" not in server:
                    server = "http://" + server
                return cls(server, value.get("username") or None, value.get("password") or None)
        except ValueError:
            pass
        raise ConfigError(
            f"{where}: invalid proxy {value!r} — use \"http://user:pass@host:port\" "
            f"or {{\"server\": \"http://host:port\", \"username\": ..., \"password\": ...}}"
        )

    @property
    def scheme(self) -> str:
        return urlsplit(self.server).scheme

    def for_playwright(self) -> dict:
        result = {"server": self.server}
        if self.username:
            result["username"] = self.username
        if self.password:
            result["password"] = self.password
        return result

    def url_with_auth(self) -> str:
        if not self.username:
            return self.server
        parts = urlsplit(self.server)
        cred = quote(self.username, safe="")
        if self.password:
            cred += ":" + quote(self.password, safe="")
        return f"{parts.scheme}://{cred}@{parts.netloc}"


@dataclass
class CrawlConfig:
    max_pages: int = 40
    max_depth: int = 2
    interval_seconds: float = 900
    same_prefix: bool = True
    include: list = field(default_factory=list)  # compiled regexes
    exclude: list = field(default_factory=list)
    track_files: bool = True
    concurrency: int = 2
    selector: str = ""


@dataclass
class Watch:
    id: str
    url: str
    name: str = ""
    type: str = "page"
    engine: str = "auto"
    selector: str = ""
    exclude_selectors: list = field(default_factory=list)
    item_selector: str = ""
    item_link_selector: str = ""
    ignore_patterns: list = field(default_factory=list)  # compiled regexes
    keywords: list = field(default_factory=list)
    only_keywords: bool = False
    track_links: bool = False
    ignore_reorder: bool = True
    interval_seconds: float = 120
    jitter_seconds: float = 10
    timeout_seconds: float = 30
    min_content_length: int = 30
    cooldown_minutes: float = 0
    error_threshold: int = 3
    silent: bool = False
    persist_session: bool = False
    verify_ssl: bool = True
    cache_bust: bool = False
    headers: dict = field(default_factory=dict)
    wait_ms: int = 0
    challenge_wait_seconds: float = 20
    enabled: bool = True
    proxy: Proxy | None = None
    crawl: CrawlConfig | None = None

    @property
    def label(self) -> str:
        return self.name or self.id

    @property
    def wait_selector(self) -> str:
        return self.item_selector if self.type == "items" else self.selector


@dataclass
class TelegramConfig:
    token: str = ""
    chat_ids: list = field(default_factory=list)
    proxy: str | None = None
    api_base: str = "https://api.telegram.org"
    commands: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.token) and bool(self.chat_ids)


@dataclass
class BrowserConfig:
    headless: bool = True
    channel: str = "chrome"
    max_pages: int = 2
    block_resources: bool = True
    restart_every: int = 200
    user_agent: str = ""


@dataclass
class Config:
    path: Path
    data_dir: Path
    timezone: str
    telegram: TelegramConfig
    browser: BrowserConfig
    watches: list
    history_days: int = 30
    daily_report_hour: int | None = 9
    notify_startup: bool = True
    max_concurrent_http: int = 8
    user_agent: str = DEFAULT_USER_AGENT
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE
    global_proxy: Proxy | None = None
    warnings: list = field(default_factory=list)

    def watch(self, watch_id: str) -> Watch | None:
        return next((w for w in self.watches if w.id == watch_id), None)


# --------------------------------------------------------------------------- helpers

def _coerce(value: Any, typ: type, where: str) -> Any:
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{where}: expected true/false, got {value!r}")
    if typ in (int, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        return typ(value)
    if typ is str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected text, got {value!r}")
        return value.strip()
    if typ is list:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
        return [v for v in value if v.strip()]
    if typ is dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected an object, got {value!r}")
        return {str(k): str(v) for k, v in value.items()}
    raise AssertionError(typ)


def _check_range(value: float, where: str, lo: float | None = None, hi: float | None = None) -> None:
    if lo is not None and value < lo:
        raise ConfigError(f"{where}: must be >= {lo:g} (got {value:g})")
    if hi is not None and value > hi:
        raise ConfigError(f"{where}: must be <= {hi:g} (got {value:g})")


def check_selector(selector: str, where: str) -> None:
    try:
        CSSSelector(selector, translator="html")
    except SelectorError as exc:
        raise ConfigError(f"{where}: invalid CSS selector {selector!r}: {exc}") from None


def _compile_patterns(patterns: list[str], where: str) -> list[re.Pattern]:
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"{where}: invalid regular expression {pattern!r}: {exc}") from None
    return compiled


def _parse_fields(raw: dict, spec: dict, where: str, warnings: list, ignore: set = frozenset()) -> dict:
    values = {}
    for key, value in raw.items():
        if key.startswith("_") or key in ignore:
            continue
        if key not in spec:
            warnings.append(f"{where}: unknown key {key!r} is ignored (typo?)")
            continue
        values[key] = _coerce(value, spec[key][0], f"{where}.{key}")
    return {key: values.get(key, default) for key, (_, default) in spec.items()}


_WATCH_SPEC = {
    "name": (str, ""),
    "type": (str, "page"),
    "engine": (str, "auto"),
    "selector": (str, ""),
    "exclude_selectors": (list, []),
    "item_selector": (str, ""),
    "item_link_selector": (str, ""),
    "ignore_patterns": (list, []),
    "keywords": (list, []),
    "only_keywords": (bool, False),
    "track_links": (bool, False),
    "ignore_reorder": (bool, True),
    "interval_seconds": (float, 120.0),
    "jitter_seconds": (float, 10.0),
    "timeout_seconds": (float, 30.0),
    "min_content_length": (int, 30),
    "cooldown_minutes": (float, 0.0),
    "error_threshold": (int, 3),
    "silent": (bool, False),
    "persist_session": (bool, False),
    "verify_ssl": (bool, True),
    "cache_bust": (bool, False),
    "headers": (dict, {}),
    "wait_ms": (int, 0),
    "challenge_wait_seconds": (float, 20.0),
    "enabled": (bool, True),
}
_WATCH_ALIASES = {
    "check_interval_seconds": "interval_seconds",
    "notification_cooldown_minutes": "cooldown_minutes",
}
_CRAWL_SPEC = {
    "max_pages": (int, 40),
    "max_depth": (int, 2),
    "interval_seconds": (float, 900.0),
    "same_prefix": (bool, True),
    "include": (list, []),
    "exclude": (list, []),
    "track_files": (bool, True),
    "concurrency": (int, 2),
    "selector": (str, ""),
}
_BROWSER_SPEC = {
    "headless": (bool, True),
    "channel": (str, "chrome"),
    "max_pages": (int, 2),
    "block_resources": (bool, True),
    "restart_every": (int, 200),
    "user_agent": (str, ""),
}
_TOP_KEYS = {
    "telegram", "timezone", "data_dir", "history_days", "daily_report_hour", "notify_startup",
    "max_concurrent_http", "user_agent", "accept_language", "global_proxy", "browser", "defaults",
    "watches", "sites", "telegram_token", "telegram_chat_id", "max_concurrent_tasks",
}


def _parse_crawl(value: Any, where: str, warnings: list) -> CrawlConfig | None:
    if value in (None, False):
        return None
    if value is True:
        value = {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected an object or true/false")
    fields = _parse_fields(value, _CRAWL_SPEC, where, warnings)
    _check_range(fields["max_pages"], f"{where}.max_pages", 1, 2000)
    _check_range(fields["max_depth"], f"{where}.max_depth", 0, 10)
    _check_range(fields["interval_seconds"], f"{where}.interval_seconds", 30)
    _check_range(fields["concurrency"], f"{where}.concurrency", 1, 10)
    if fields["selector"]:
        check_selector(fields["selector"], f"{where}.selector")
    fields["include"] = _compile_patterns(fields["include"], f"{where}.include")
    fields["exclude"] = _compile_patterns(fields["exclude"], f"{where}.exclude")
    return CrawlConfig(**fields)


def _parse_watch(raw: Any, defaults: dict, index: int, warnings: list) -> Watch:
    where = f"watches[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each watch must be an object")
    merged = dict(defaults)
    for key, value in raw.items():
        merged[_WATCH_ALIASES.get(key, key)] = value

    watch_id = merged.get("id")
    if not isinstance(watch_id, str) or not _ID_RE.match(watch_id):
        raise ConfigError(f"{where}.id: required; use 1-64 latin letters, digits, '_', '-' or '.' (got {watch_id!r})")
    where = f"watch {watch_id!r}"
    url = merged.get("url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"{where}.url: must be a full http(s) URL (got {url!r})")

    fields = _parse_fields(merged, _WATCH_SPEC, where, warnings, ignore={"id", "url", "proxy", "crawl"})
    if fields["type"] not in WATCH_TYPES:
        raise ConfigError(f"{where}.type: must be one of {', '.join(WATCH_TYPES)}")
    if fields["engine"] not in ENGINES:
        raise ConfigError(f"{where}.engine: must be one of {', '.join(ENGINES)}")
    if fields["type"] == "items" and not fields["item_selector"]:
        raise ConfigError(f"{where}: type \"items\" needs an \"item_selector\"")
    for key in ("selector", "item_selector", "item_link_selector"):
        if fields[key]:
            check_selector(fields[key], f"{where}.{key}")
    for selector in fields["exclude_selectors"]:
        check_selector(selector, f"{where}.exclude_selectors")
    _check_range(fields["interval_seconds"], f"{where}.interval_seconds", 5)
    _check_range(fields["jitter_seconds"], f"{where}.jitter_seconds", 0)
    _check_range(fields["timeout_seconds"], f"{where}.timeout_seconds", 3, 300)
    _check_range(fields["min_content_length"], f"{where}.min_content_length", 0)
    _check_range(fields["cooldown_minutes"], f"{where}.cooldown_minutes", 0)
    _check_range(fields["error_threshold"], f"{where}.error_threshold", 1)
    _check_range(fields["wait_ms"], f"{where}.wait_ms", 0, 120000)
    _check_range(fields["challenge_wait_seconds"], f"{where}.challenge_wait_seconds", 0, 180)
    fields["ignore_patterns"] = _compile_patterns(fields["ignore_patterns"], f"{where}.ignore_patterns")

    crawl = _parse_crawl(merged.get("crawl"), f"{where}.crawl", warnings)
    if crawl and fields["type"] != "page":
        raise ConfigError(f"{where}.crawl: crawling is only supported for type \"page\"")
    if fields["type"] == "feed" and fields["engine"] == "browser":
        warnings.append(f"{where}: feeds are plain XML; engine \"http\" is usually enough")
    return Watch(
        id=watch_id,
        url=url.strip(),
        proxy=Proxy.parse(merged.get("proxy"), f"{where}.proxy"),
        crawl=crawl,
        **fields,
    )


def parse_config(raw: Any, path: Path) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("The config file must contain a JSON object")
    raw = dict(raw)
    warnings: list[str] = []
    for key in raw:
        if not key.startswith("_") and key not in _TOP_KEYS:
            warnings.append(f"unknown top-level key {key!r} is ignored (typo?)")

    # --- telegram (new nested form, or the old flat keys)
    tg_raw = raw.get("telegram") or {}
    if not isinstance(tg_raw, dict):
        raise ConfigError("telegram: expected an object")
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg_raw.get("token") or raw.get("telegram_token") or ""
    chat_ids = tg_raw.get("chat_ids", tg_raw.get("chat_id"))
    if chat_ids is None:
        chat_ids = raw.get("telegram_chat_id")
    if os.environ.get("TELEGRAM_CHAT_ID"):
        chat_ids = os.environ["TELEGRAM_CHAT_ID"].split(",")
    if not isinstance(chat_ids, list):
        chat_ids = [chat_ids] if chat_ids not in (None, "") else []
    chat_ids = [str(c).strip() for c in chat_ids if str(c).strip() not in PLACEHOLDER_CHATS]
    token = str(token).strip()
    if token in PLACEHOLDER_TOKENS:
        token = ""
    if token and not re.match(r"^\d+:[\w-]{20,}$", token):
        raise ConfigError("telegram.token: does not look like a bot token from @BotFather (123456:ABC-...)")
    for key in tg_raw:
        if not key.startswith("_") and key not in {"token", "chat_ids", "chat_id", "proxy", "api_base", "commands"}:
            warnings.append(f"telegram: unknown key {key!r} is ignored (typo?)")
    tg_proxy = tg_raw.get("proxy") or None
    if tg_proxy is not None:
        if not isinstance(tg_proxy, str) or "://" not in tg_proxy:
            raise ConfigError("telegram.proxy: use a URL such as \"http://127.0.0.1:8080\" or \"socks5://127.0.0.1:1080\"")
    telegram = TelegramConfig(
        token=token,
        chat_ids=chat_ids,
        proxy=tg_proxy,
        api_base=str(tg_raw.get("api_base") or "https://api.telegram.org").rstrip("/"),
        commands=_coerce(tg_raw.get("commands", True), bool, "telegram.commands"),
    )

    # --- browser
    browser_raw = raw.get("browser") or {}
    if not isinstance(browser_raw, dict):
        raise ConfigError("browser: expected an object")
    if "max_concurrent_tasks" in raw and "max_pages" not in browser_raw:
        browser_raw = {**browser_raw, "max_pages": raw["max_concurrent_tasks"]}
    browser = BrowserConfig(**_parse_fields(browser_raw, _BROWSER_SPEC, "browser", warnings))
    _check_range(browser.max_pages, "browser.max_pages", 1, 16)
    _check_range(browser.restart_every, "browser.restart_every", 10)

    # --- watches
    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults: expected an object")
    defaults = {_WATCH_ALIASES.get(k, k): v for k, v in defaults.items() if not k.startswith("_")}
    watch_list = raw.get("watches", raw.get("sites"))
    if not isinstance(watch_list, list) or not watch_list:
        raise ConfigError("watches: add at least one site to monitor")
    watches = [_parse_watch(item, defaults, i, warnings) for i, item in enumerate(watch_list)]
    seen: set[str] = set()
    for watch in watches:
        if watch.id in seen:
            raise ConfigError(f"watch id {watch.id!r} is used more than once")
        seen.add(watch.id)

    base_dir = path.parent
    data_dir = Path(_coerce(raw.get("data_dir", "data"), str, "data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    report_hour = raw.get("daily_report_hour", 9)
    if report_hour is not None:
        report_hour = int(_coerce(report_hour, int, "daily_report_hour"))
        _check_range(report_hour, "daily_report_hour", 0, 23)
    history_days = _coerce(raw.get("history_days", 30), int, "history_days")
    _check_range(history_days, "history_days", 1)
    max_http = _coerce(raw.get("max_concurrent_http", 8), int, "max_concurrent_http")
    _check_range(max_http, "max_concurrent_http", 1, 64)

    return Config(
        path=path,
        data_dir=data_dir,
        timezone=_coerce(raw.get("timezone", "Asia/Tehran"), str, "timezone"),
        telegram=telegram,
        browser=browser,
        watches=watches,
        history_days=history_days,
        daily_report_hour=report_hour,
        notify_startup=_coerce(raw.get("notify_startup", True), bool, "notify_startup"),
        max_concurrent_http=max_http,
        user_agent=_coerce(raw.get("user_agent") or DEFAULT_USER_AGENT, str, "user_agent"),
        accept_language=_coerce(raw.get("accept_language") or DEFAULT_ACCEPT_LANGUAGE, str, "accept_language"),
        global_proxy=Proxy.parse(raw.get("global_proxy"), "global_proxy"),
        warnings=warnings,
    )


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from None
    return parse_config(raw, path)

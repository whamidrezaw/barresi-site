"""Small shared helpers: text normalisation, escaping, hashing and time formatting."""

from __future__ import annotations

import hashlib
import html
import logging
import re
import unicodedata
from datetime import datetime, tzinfo

log = logging.getLogger("webmonitor")

_WS_RE = re.compile(r"\s+")
# Invisible formatting characters that only add noise to comparisons.
# ZWNJ (U+200C) and ZWJ (U+200D) are meaningful in Persian and are kept.
_INVISIBLE_RE = re.compile("[­​‎‏‪-‮⁦-⁩﻿]")
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def clean_text(text: str | None) -> str:
    """NFC-normalise, drop invisible characters and collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _INVISIBLE_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def has_alnum(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def match_key(text: str) -> str:
    """Normalise text for keyword matching (case, Arabic/Persian letters, digits, ZWNJ)."""
    text = text.lower().translate(_DIGITS)
    text = text.replace("ي", "ی").replace("ى", "ی").replace("ك", "ک").replace("‌", " ")
    return _WS_RE.sub(" ", text)


def find_keywords(keywords: list[str], *texts: str) -> list[str]:
    if not keywords:
        return []
    haystack = match_key(" \n ".join(t for t in texts if t))
    return [kw for kw in keywords if match_key(kw) in haystack]


def esc(text: object) -> str:
    """Escape text for Telegram HTML (and regular HTML) bodies."""
    return html.escape(str(text), quote=False)


def esc_attr(text: object) -> str:
    return html.escape(str(text), quote=True)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to the Persian (Jalali) calendar."""
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def load_timezone(name: str | None) -> tzinfo:
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception as exc:  # missing tzdata on Windows, typo, ...
            log.warning("Unknown timezone %r (%s); using the system timezone", name, exc)
    return datetime.now().astimezone().tzinfo


def format_ts(ts: float, tz: tzinfo) -> str:
    dt = datetime.fromtimestamp(ts, tz)
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy}/{jm:02d}/{jd:02d} – {dt:%H:%M:%S} ({dt:%Y-%m-%d})"


def human_duration(seconds: float) -> str:
    """Persian human-readable duration."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} ثانیه"
    minutes, rest = divmod(seconds, 60)
    if minutes < 10 and rest:
        return f"{minutes} دقیقه و {rest} ثانیه"
    if minutes < 60:
        return f"{minutes} دقیقه"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ساعت" + (f" و {minutes} دقیقه" if minutes else "")
    days, hours = divmod(hours, 24)
    return f"{days} روز" + (f" و {hours} ساعت" if hours else "")


def first_line(text: object, limit: int = 300) -> str:
    """First non-empty line of an exception message (Playwright errors are multi-line)."""
    for line in str(text).splitlines():
        line = line.strip()
        if line:
            return truncate(line, limit)
    return type(text).__name__ if isinstance(text, BaseException) else ""

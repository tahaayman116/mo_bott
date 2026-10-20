"""Small pure helpers: phone normalization, interval parsing, link extraction."""
from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s<>()\u0600-\u06FF]+", re.IGNORECASE)
_TRAILING = ".,;:!?)\\]}'\"*_~ \t\r\n"


def normalize_phone(raw: str) -> str:
    """'+20 100 123 4567' -> '+201001234567'. Returns '' if invalid."""
    digits = re.sub(r"\D", "", raw or "")
    if raw and raw.strip().startswith("+"):
        phone = "+" + digits
    else:
        phone = "+" + digits if digits else ""
    if not re.fullmatch(r"\+\d{7,15}", phone or ""):
        return ""
    return phone


JOIN_MIN_INTERVAL_SEC = 10
JOIN_MAX_INTERVAL_SEC = 24 * 60 * 60


def parse_interval_minutes(raw: str) -> int | None:
    """'5', '5د', '5 دقايق' -> 5. Valid range 1..1440, else None."""
    m = re.search(r"\d+", raw or "")
    if not m:
        return None
    try:
        v = int(m.group(0))
    except ValueError:
        return None
    if 1 <= v <= 1440:
        return v
    return None


def parse_join_interval(raw: str) -> int | None:
    """Parse a join interval into seconds.

    '10s', '10 sec', '10 ثواني', '10 ثانية' -> seconds.
    '5', '5 دقايق' -> minutes (300s). Valid range 10s..24h, else None.
    """
    text = (raw or "").strip().lower()
    m = re.search(r"\d+", text)
    if not m:
        return None
    try:
        v = int(m.group(0))
    except ValueError:
        return None
    is_seconds = "ث" in text or bool(re.search(
        r"(?<![a-z])(s|sec|secs|second|seconds)(?![a-z])", text))
    if is_seconds:
        seconds = v
    else:
        if not 1 <= v <= 1440:
            return None
        seconds = v * 60
    if JOIN_MIN_INTERVAL_SEC <= seconds <= JOIN_MAX_INTERVAL_SEC:
        return seconds
    return None


def _minutes_ar(minutes: int) -> str:
    if minutes == 1:
        return "دقيقة"
    if minutes == 2:
        return "دقيقتين"
    if 3 <= minutes <= 10:
        return f"{minutes} دقايق"
    return f"{minutes} دقيقة"


def _hours_ar(hours: int) -> str:
    if hours == 1:
        return "ساعة"
    if hours == 2:
        return "ساعتين"
    if 3 <= hours <= 10:
        return f"{hours} ساعات"
    return f"{hours} ساعة"


def format_duration_ar(seconds: int) -> str:
    """10 -> '10 ثانية', 300 -> '5 دقايق', 3600 -> 'ساعة'."""
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        hours, rem = divmod(seconds, 3600)
        text = _hours_ar(hours)
        if rem == 0:
            return text
        minutes, secs = divmod(rem, 60)
        if minutes:
            return f"{text} و{_minutes_ar(minutes)}"
        return f"{text} و{secs} ثانية"
    if seconds < 60:
        return f"{seconds} ثانية"
    minutes, rem = divmod(seconds, 60)
    if rem:
        return f"{_minutes_ar(minutes)} و{rem} ثانية"
    return _minutes_ar(minutes)


def extract_links(text: str) -> list[str]:
    """Pull http(s) URLs out of free text, de-duplicated, order kept."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(_TRAILING)
        key = url.lower()
        if url and key not in seen:
            seen.add(key)
            found.append(url)
    return found


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Split a long message on line boundaries for Telegram's 4096 cap."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.splitlines(keepends=True):
        if cur_len + len(line) > limit and cur:
            parts.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line)
    if cur:
        parts.append("".join(cur))
    return parts or [text]

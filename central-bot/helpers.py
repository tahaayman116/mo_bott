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

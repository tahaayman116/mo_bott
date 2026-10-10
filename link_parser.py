"""Telegram group-link extraction (Phase 1: shape-only, NO API verification).

Covers:
- https://t.me/+HASH , https://t.me/joinchat/HASH , https://t.me/username
- telegram.me equivalents, http/https, www., case-insensitive domain
- multiple links per message, captions (caller passes caption as text),
  hidden URLs via message entities (caller passes entity_urls),
  surrounding punctuation, normalization without corrupting hashes.

Phase-1 policy (per user decision): NO network verification. Everything is
classified by shape only. Ambiguous links are KEPT as needs-review, never
silently discarded. Clearly non-group Telegram paths (stickers, proxy, ...)
are labelled other_telegram so the collector does NOT notify them as groups.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# Matches t.me / telegram.me URLs with or without scheme/www.
# Negative lookbehind prevents matching "t.me/..." embedded inside a
# longer non-telegram URL such as https://example.com/t.me/fake
# (preceded by / . : @ or alphanumeric means it is part of another URL/host).
_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9./:@_-])((?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>\"'\)\]]*)"
)

# Telegram public usernames: 5-32 chars, letters/digits/underscore.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")

# Paths that are definitely NOT groups (official Telegram pages/features).
_RESERVED_FIRST = {
    "joinchat", "s", "addlist", "share", "iv", "proxy", "socks",
    "bg", "giftcode", "addstickers", "addemoji", "setlanguage",
    "confirmphone", "login", "stat", "addtheme", "join",
}

_TRAILING_STRIP = ".,;:!?)\\]}'\"“”‘’…*_~ \t\r\n\u061b\u061f\u060c"
_LEADING_STRIP = "(<[{'\"“”‘’ \t\r\n"

_KIND_PRIVATE = "private_invite"
_KIND_PUBLIC = "public_username"
_KIND_FOLDER = "folder"
_KIND_OTHER = "other_telegram"

_STATUS_PRIVATE_UNVERIFIED = "private_invite_unverified"
_STATUS_PUBLIC_REVIEW = "public_link_needs_review"
_STATUS_FOLDER = "folder_link_optional"
_STATUS_OTHER = "non_group_or_unrecognized"


@dataclass(frozen=True)
class ParsedLink:
    original_url: str
    normalized_url: str
    kind: str
    status: str


def _strip_surrounding(raw: str) -> str:
    s = raw.strip()
    s = s.lstrip(_LEADING_STRIP)
    # Strip trailing punctuation repeatedly, but keep a lone "+" path intact.
    while s and s[-1] in _TRAILING_STRIP:
        # Do not strip '/' here; handled in normalize (trailing slash rule).
        if s[-1] == "/":
            break
        s = s[:-1]
    return s.strip()


def normalize_url(raw: str) -> str:
    """Normalize equivalent telegram URLs without corrupting invite hashes.

    - telegram.me -> t.me, strip www., force https://
    - domain lowercased only; path case PRESERVED (hashes are case-sensitive)
    - strip trailing '/' (except root), drop fragment, keep query
    """
    s = _strip_surrounding(raw)
    if not s:
        return s
    if not re.match(r"(?i)^https?://", s):
        s = "https://" + s
    try:
        parts = urlparse(s)
    except Exception:
        return s
    host = (parts.hostname or "").lower()
    if host in ("telegram.me", "www.telegram.me", "www.t.me"):
        host = "t.me"
    if host != "t.me":
        # Not a telegram link; return best-effort normalized form.
        return urlunparse(("https", host, parts.path or "", "", parts.query or "", ""))
    path = parts.path or ""
    # Remove trailing slash for dedup (keep root "/").
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    netloc = "t.me"
    # Preserve explicit port? Telegram never uses one; drop it for dedup.
    return urlunparse(("https", netloc, path, "", parts.query or "", ""))


def is_telegram_url(raw: str) -> bool:
    s = raw.strip().lower()
    return ("t.me/" in s) or ("t.me" in s and s.rstrip("/").endswith("t.me")) or ("telegram.me/" in s)


def classify(normalized: str) -> tuple[str, str]:
    """Classify by shape only. Returns (kind, status)."""
    try:
        path = urlparse(normalized).path or ""
    except Exception:
        return _KIND_OTHER, _STATUS_OTHER
    p = path.lstrip("/")
    if not p:
        return _KIND_OTHER, _STATUS_OTHER
    # Private invites
    if p == "+" or p.startswith("+/"):
        return _KIND_OTHER, _STATUS_OTHER  # "+" with no hash -> invalid, keep
    if p.startswith("+"):
        return _KIND_PRIVATE, _STATUS_PRIVATE_UNVERIFIED
    if p == "joinchat" or p == "joinchat/":
        return _KIND_OTHER, _STATUS_OTHER
    if p.startswith("joinchat/"):
        return _KIND_PRIVATE, _STATUS_PRIVATE_UNVERIFIED
    if p == "addlist" or p.startswith("addlist/"):
        return _KIND_FOLDER, _STATUS_FOLDER
    # t.me/s/username preview links -> treat as public username
    if p == "s" or p == "s/":
        return _KIND_OTHER, _STATUS_OTHER
    if p.startswith("s/"):
        rest = p[2:].split("/")[0].split("?")[0]
        if rest and _USERNAME_RE.match(rest):
            return _KIND_PUBLIC, _STATUS_PUBLIC_REVIEW
        return _KIND_OTHER, _STATUS_OTHER
    first = p.split("/")[0].split("?")[0]
    low = first.lower()
    if low in _RESERVED_FIRST:
        return _KIND_OTHER, _STATUS_OTHER
    # Public username (may be group/channel/user/bot - indistinguishable offline)
    core = first
    if core and _USERNAME_RE.match(core):
        return _KIND_PUBLIC, _STATUS_PUBLIC_REVIEW
    return _KIND_OTHER, _STATUS_OTHER


def extract_raw_urls(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in _URL_RE.finditer(text):
        raw = _strip_surrounding(m.group(1))
        if raw:
            out.append(raw)
    return out


def parse_message_text(
    text: str | None, entity_urls: list[str] | None = None
) -> list[ParsedLink]:
    """Extract + normalize + classify all telegram links in a message.

    Args:
        text: message text or media caption (already combined by caller).
        entity_urls: hidden clickable URL targets from message entities
            (e.g. MessageEntityTextUrl.url). Any-domain; only telegram ones kept.
    Returns:
        Unique (by normalized_url) ParsedLink list, order-preserved.
    """
    raws = extract_raw_urls(text)
    if entity_urls:
        for u in entity_urls:
            if u and isinstance(u, str) and is_telegram_url(u):
                cleaned = _strip_surrounding(u)
                if cleaned:
                    raws.append(cleaned)
    seen: dict[str, ParsedLink] = {}
    for raw in raws:
        norm = normalize_url(raw)
        if not norm or "t.me" not in urlparse(norm).netloc:
            continue
        kind, status = classify(norm)
        if norm not in seen:
            seen[norm] = ParsedLink(
                original_url=raw, normalized_url=norm, kind=kind, status=status
            )
    return list(seen.values())


def is_group_candidate(link: ParsedLink) -> bool:
    """True for links the collector should notify as group candidates.

    Includes folder links: a folder holds groups inside, so the user
    wants to see it in Saved Messages too.
    """
    return link.kind in (_KIND_PRIVATE, _KIND_PUBLIC, _KIND_FOLDER)


def redact_url_for_logs(url: str) -> str:
    """Redact private invite hashes for logs. Public usernames are shown fully."""
    try:
        parts = urlparse(url)
        path = parts.path or ""
        p = path.lstrip("/")
        if p.startswith("+") and len(p) > 5:
            return f"https://t.me/+{p[1:5]}***"
        if p.startswith("joinchat/") and len(p) > 14:
            return "https://t.me/joinchat/***"
        return url
    except Exception:
        return "https://t.me/***"


def extract_invite_hash(normalized_url: str) -> str | None:
    """Return the invite hash for private-invite URLs (without leading '+').

    - https://t.me/+HASH -> 'HASH'
    - https://t.me/joinchat/HASH -> 'HASH'
    Returns None for non-invite URLs or empty hashes.
    """
    try:
        p = (urlparse(normalized_url).path or "").lstrip("/")
    except Exception:
        return None
    if p.startswith("+"):
        h = p[1:].split("/")[0].split("?")[0]
        return h or None
    if p.startswith("joinchat/"):
        h = p[len("joinchat/"):].split("/")[0].split("?")[0]
        return h or None
    return None


def extract_username(normalized_url: str) -> str | None:
    """Return the public username for public-username URLs (handles t.me/s/ prefix)."""
    try:
        p = (urlparse(normalized_url).path or "").lstrip("/")
    except Exception:
        return None
    if p.startswith("s/"):
        p = p[2:]
    first = p.split("/")[0].split("?")[0]
    if first and _USERNAME_RE.match(first) and first.lower() not in _RESERVED_FIRST:
        return first
    return None


def extract_folder_slug(normalized_url: str) -> str | None:
    """Return the folder slug for t.me/addlist/SLUG URLs."""
    try:
        p = (urlparse(normalized_url).path or "").lstrip("/")
    except Exception:
        return None
    if p == "addlist" or p == "addlist/":
        return None
    if p.startswith("addlist/"):
        slug = p[len("addlist/"):].split("/")[0].split("?")[0]
        return slug or None
    return None

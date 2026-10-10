"""SQLite persistence (Phase 2). Stdlib sqlite3 only - portable Windows/Termux.

Tables: links, discovery_events, known_chats, app_settings, audit_events,
        join_requests (added in schema v2).
- Parameterized queries everywhere.
- Transaction per state change.
- UNIQUE(normalized_url) for dedup.
- Re-notify support: links.last_notified_at + links.notify_count.
- Join queue: join_requests with explicit pending -> final statuses.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from link_parser import ParsedLink

SCHEMA_VERSION = 2

# Join-request lifecycle statuses (stored as plain text):
#   pending_approval -> (confirm) -> joining -> joined | join_requested
#                                            -> already_member
#                                            -> failed_expired | failed_invalid
#                                            -> failed_denied | failed_flood_deferred
#                                            -> failed_uncertain (outcome unknown, e.g. crash mid-join)
#   pending_approval -> (cancel) -> cancelled
JR_PENDING = "pending_approval"
JR_JOINING = "joining"
JR_JOINED = "joined"
JR_REQUESTED = "join_requested"  # approval-mode group: request sent, waiting admin
JR_ALREADY = "already_member"
JR_EXPIRED = "failed_expired"
JR_INVALID = "failed_invalid"
JR_DENIED = "failed_denied"
JR_FLOOD_DEFERRED = "failed_flood_deferred"
JR_UNCERTAIN = "failed_uncertain"
JR_CANCELLED = "cancelled"

JR_FINAL = frozenset({
    JR_JOINED, JR_REQUESTED, JR_ALREADY, JR_EXPIRED, JR_INVALID,
    JR_DENIED, JR_FLOOD_DEFERRED, JR_UNCERTAIN, JR_CANCELLED,
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_url TEXT UNIQUE NOT NULL,
    original_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    discovery_count INTEGER NOT NULL DEFAULT 1,
    last_source_title TEXT,
    last_source_id TEXT,
    need_review INTEGER NOT NULL DEFAULT 1,
    last_notified_at TEXT,
    notify_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id INTEGER NOT NULL REFERENCES links(id),
    source_chat_id TEXT,
    source_title TEXT,
    source_username TEXT,
    discovered_at TEXT NOT NULL,
    snippet TEXT
);
CREATE TABLE IF NOT EXISTS known_chats (
    chat_id TEXT PRIMARY KEY,
    title TEXT,
    username TEXT,
    chat_type TEXT,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_last_seen ON links(last_seen);
CREATE INDEX IF NOT EXISTS idx_events_link ON discovery_events(link_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON discovery_events(discovered_at);
CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    preview_title TEXT,
    preview_type TEXT,
    preview_detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_join_status ON join_requests(status);
CREATE INDEX IF NOT EXISTS idx_join_url ON join_requests(normalized_url);
"""


_MIGRATIONS = {
    # v1 -> v2: join queue for Phase 2 (CREATE IF NOT EXISTS is idempotent,
    # so re-running on an already-migrated DB is safe).
    1: """
CREATE TABLE IF NOT EXISTS join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_url TEXT NOT NULL,
    kind TEXT NOT NULL,
    preview_title TEXT,
    preview_type TEXT,
    preview_detail TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_join_status ON join_requests(status);
CREATE INDEX IF NOT EXISTS idx_join_url ON join_requests(normalized_url);
""",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def init_db(db_path: Path) -> None:
    con = _connect(db_path)
    try:
        con.executescript(_SCHEMA_SQL)
        row = con.execute(
            "SELECT value FROM app_settings WHERE key='schema_version'"
        ).fetchone()
        current = int(row["value"]) if row is not None else 0
        if current < 1:
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            current = SCHEMA_VERSION
        # Stepwise migrations for existing DBs (e.g. v1 created in Phase 1).
        while current < SCHEMA_VERSION:
            sql = _MIGRATIONS.get(current)
            if sql is None:  # pragma: no cover - defensive, should not happen
                break
            con.executescript(sql)
            current += 1
            con.execute(
                "INSERT INTO app_settings(key, value) VALUES('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )
        con.commit()
    finally:
        con.close()


def add_or_update_link(
    db_path: Path,
    parsed: ParsedLink,
    source_chat_id: str | None,
    source_title: str | None,
    source_username: str | None,
    discovered_at: str | None = None,
    snippet: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Insert or bump a link. Returns (is_new, record dict). Transactional."""
    ts = discovered_at or utcnow_iso()
    short_snippet = (snippet or "")[:300]
    need_review = 1 if parsed.status in (
        "private_invite_unverified", "public_link_needs_review",
        "folder_link_optional", "non_group_or_unrecognized",
    ) else 1
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM links WHERE normalized_url=?", (parsed.normalized_url,)
        )
        row = cur.fetchone()
        if row is None:
            cur = con.execute(
                """INSERT INTO links(normalized_url, original_url, kind, status,
                   first_seen, last_seen, discovery_count, last_source_title,
                   last_source_id, need_review)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (parsed.normalized_url, parsed.original_url, parsed.kind,
                 parsed.status, ts, ts, 1, source_title, source_chat_id, need_review),
            )
            link_id = cur.lastrowid
            is_new = True
        else:
            link_id = row["id"]
            con.execute(
                """UPDATE links SET last_seen=?, discovery_count=discovery_count+1,
                   last_source_title=?, last_source_id=? WHERE id=?""",
                (ts, source_title, source_chat_id, link_id),
            )
            is_new = False
        con.execute(
            """INSERT INTO discovery_events(link_id, source_chat_id, source_title,
               source_username, discovered_at, snippet) VALUES(?,?,?,?,?,?)""",
            (link_id, source_chat_id, source_title, source_username, ts, short_snippet),
        )
        con.commit()
        rec = dict(con.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone())
        return is_new, rec
    finally:
        con.close()


def mark_notified(db_path: Path, normalized_url: str, when_iso: str | None = None) -> None:
    ts = when_iso or utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute(
            "UPDATE links SET last_notified_at=?, notify_count=notify_count+1 WHERE normalized_url=?",
            (ts, normalized_url),
        )
        con.commit()
    finally:
        con.close()


def get_link(db_path: Path, normalized_url: str) -> dict[str, Any] | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM links WHERE normalized_url=?", (normalized_url,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_links(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM links ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_stats(db_path: Path) -> dict[str, int]:
    con = _connect(db_path)
    try:
        unique_links = con.execute("SELECT COUNT(*) c FROM links").fetchone()["c"]
        pending_review = con.execute(
            "SELECT COUNT(*) c FROM links WHERE need_review=1"
        ).fetchone()["c"]
        total_events = con.execute("SELECT COUNT(*) c FROM discovery_events").fetchone()["c"]
        notified = con.execute(
            "SELECT COUNT(*) c FROM links WHERE notify_count>0"
        ).fetchone()["c"]
        return {
            "unique_links": unique_links,
            "pending_review": pending_review,
            "total_events": total_events,
            "notified": notified,
        }
    finally:
        con.close()


def upsert_known_chat(db_path: Path, chat_id: str, title: str | None,
                      username: str | None, chat_type: str | None) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            """INSERT INTO known_chats(chat_id, title, username, chat_type, last_seen)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
               username=excluded.username, chat_type=excluded.chat_type,
               last_seen=excluded.last_seen""",
            (chat_id, title, username, chat_type, utcnow_iso()),
        )
        con.commit()
    finally:
        con.close()


def set_setting(db_path: Path, key: str, value: str) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO app_settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def get_setting(db_path: Path, key: str) -> str | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        con.close()


def log_audit(db_path: Path, category: str, message: str) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT INTO audit_events(ts, category, message) VALUES(?,?,?)",
            (utcnow_iso(), category, message[:1000]),
        )
        con.commit()
    finally:
        con.close()


def backup_db(db_path: Path, backup_path: Path) -> Path:
    """Online-safe backup using sqlite3 backup API. Returns backup file path."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


# ---------------- Join requests (Phase 2 queue) ----------------

def create_join_request(db_path: Path, normalized_url: str, kind: str,
                        preview_title: str | None, preview_type: str | None,
                        preview_detail: str | None) -> dict[str, Any]:
    con = _connect(db_path)
    try:
        cur = con.execute(
            """INSERT INTO join_requests(normalized_url, kind, preview_title,
               preview_type, preview_detail, status, created_at, attempts)
               VALUES(?,?,?,?,?,?,?,0)""",
            (normalized_url, kind, preview_title, preview_type,
             (preview_detail or "")[:1000], JR_PENDING, utcnow_iso()),
        )
        con.commit()
        return dict(con.execute(
            "SELECT * FROM join_requests WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        con.close()


def get_join_request(db_path: Path, request_id: int) -> dict[str, Any] | None:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM join_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_join_requests(db_path: Path, status: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
    con = _connect(db_path)
    try:
        if status:
            rows = con.execute(
                "SELECT * FROM join_requests WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM join_requests ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def count_pending_joins(db_path: Path) -> int:
    con = _connect(db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) c FROM join_requests WHERE status=?",
            (JR_PENDING,)).fetchone()["c"]
    finally:
        con.close()


def set_join_status(db_path: Path, request_id: int, status: str,
                    result: str | None = None, error: str | None = None,
                    bump_attempts: bool = False) -> dict[str, Any] | None:
    con = _connect(db_path)
    try:
        if bump_attempts:
            con.execute(
                """UPDATE join_requests SET status=?, decided_at=?,
                   result=?, error=?, attempts=attempts+1 WHERE id=?""",
                (status, utcnow_iso(),
                 (result or "")[:1000] if result else None,
                 (error or "")[:1000] if error else None, request_id),
            )
        else:
            con.execute(
                """UPDATE join_requests SET status=?, decided_at=?,
                   result=?, error=? WHERE id=?""",
                (status, utcnow_iso(),
                 (result or "")[:1000] if result else None,
                 (error or "")[:1000] if error else None, request_id),
            )
        con.commit()
        row = con.execute(
            "SELECT * FROM join_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def recover_stale_joining(db_path: Path) -> list[dict[str, Any]]:
    """Mark rows left in 'joining' (crash mid-join) as uncertain.

    Returns the affected rows so the caller can notify the user to verify
    membership manually instead of blindly retrying.
    """
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM join_requests WHERE status=?", (JR_JOINING,)).fetchall()
        out = [dict(r) for r in rows]
        if out:
            con.execute(
                """UPDATE join_requests SET status=?, decided_at=?,
                   error=? WHERE status=?""",
                (JR_UNCERTAIN, utcnow_iso(),
                 "Restart during join - outcome unknown, verify membership before retrying",
                 JR_JOINING),
            )
            con.commit()
        return out
    finally:
        con.close()

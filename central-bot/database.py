"""SQLite storage for the central bot.

Tables:
  accounts(phone TEXT PK, session_file TEXT, display_name TEXT,
           added_at TEXT, last_used_at TEXT)
  kv(key TEXT PK, value TEXT)  -- 'active_phone', daily counters, etc.
  collected(phone TEXT, kind TEXT, title TEXT, link TEXT, collected_at TEXT)
  broadcasts(id INTEGER PK, phone TEXT, text TEXT, interval_min INTEGER,
             status TEXT, total INTEGER, done_ok INTEGER, done_fail INTEGER,
             created_at TEXT, finished_at TEXT)
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    con = _connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
              phone TEXT PRIMARY KEY,
              session_file TEXT NOT NULL,
              display_name TEXT DEFAULT '',
              added_at TEXT NOT NULL,
              last_used_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS kv (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collected (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT NOT NULL,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              link TEXT DEFAULT '',
              collected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_collected_phone ON collected(phone);
            CREATE TABLE IF NOT EXISTS broadcasts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              phone TEXT NOT NULL,
              text TEXT NOT NULL,
              interval_min INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'running',
              total INTEGER NOT NULL DEFAULT 0,
              done_ok INTEGER NOT NULL DEFAULT 0,
              done_fail INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              finished_at TEXT DEFAULT ''
            );
            """
        )
        con.commit()
    finally:
        con.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- accounts ----
def add_account(db_path: Path, phone: str, session_file: str,
                display_name: str = "") -> None:
    con = _connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO accounts(phone, session_file, display_name, added_at)"
            " VALUES(?,?,?,?)",
            (phone, session_file, display_name, now_iso()),
        )
        con.commit()
    finally:
        con.close()


def remove_account(db_path: Path, phone: str) -> None:
    con = _connect(db_path)
    try:
        con.execute("DELETE FROM accounts WHERE phone=?", (phone,))
        con.execute("DELETE FROM collected WHERE phone=?", (phone,))
        r = con.execute("SELECT value FROM kv WHERE key='active_phone'").fetchone()
        if r and r["value"] == phone:
            con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES('active_phone','')")
        con.commit()
    finally:
        con.close()


def list_accounts(db_path: Path) -> list[dict]:
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT phone, session_file, display_name, added_at, last_used_at"
            " FROM accounts ORDER BY added_at").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_account(db_path: Path, phone: str) -> dict | None:
    con = _connect(db_path)
    try:
        r = con.execute("SELECT * FROM accounts WHERE phone=?", (phone,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def touch_account(db_path: Path, phone: str) -> None:
    con = _connect(db_path)
    try:
        con.execute("UPDATE accounts SET last_used_at=? WHERE phone=?",
                    (now_iso(), phone))
        con.commit()
    finally:
        con.close()


# ---- kv ----
def kv_get(db_path: Path, key: str, default: str = "") -> str:
    con = _connect(db_path)
    try:
        r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default
    finally:
        con.close()


def kv_set(db_path: Path, key: str, value: str) -> None:
    con = _connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)",
                    (key, value))
        con.commit()
    finally:
        con.close()


def get_active_phone(db_path: Path) -> str:
    return kv_get(db_path, "active_phone", "")


def set_active_phone(db_path: Path, phone: str) -> None:
    kv_set(db_path, "active_phone", phone)


def today_key(prefix: str) -> str:
    return f"{prefix}:{date.today().isoformat()}"


def counter_get(db_path: Path, prefix: str) -> int:
    try:
        return int(kv_get(db_path, today_key(prefix), "0") or "0")
    except ValueError:
        return 0


def counter_inc(db_path: Path, prefix: str) -> int:
    v = counter_get(db_path, prefix) + 1
    kv_set(db_path, today_key(prefix), str(v))
    return v


# ---- collected cache ----
def save_collected(db_path: Path, phone: str,
                   items: list[dict]) -> None:
    con = _connect(db_path)
    try:
        con.execute("DELETE FROM collected WHERE phone=?", (phone,))
        ts = now_iso()
        con.executemany(
            "INSERT INTO collected(phone, kind, title, link, collected_at)"
            " VALUES(?,?,?,?,?)",
            [(phone, i.get("kind", ""), i.get("title", ""),
              i.get("link", ""), ts) for i in items],
        )
        con.commit()
    finally:
        con.close()


# ---- broadcasts ----
def create_broadcast(db_path: Path, phone: str, text: str,
                     interval_min: int, total: int) -> int:
    con = _connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO broadcasts(phone, text, interval_min, status, total, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (phone, text, interval_min, "running", total, now_iso()),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def update_broadcast(db_path: Path, bid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    con = _connect(db_path)
    try:
        con.execute(f"UPDATE broadcasts SET {cols} WHERE id=?",
                    (*fields.values(), bid))
        con.commit()
    finally:
        con.close()

"""Join logic for the active session: classify links, paced auto-join, manual join.

Link kinds: public username (t.me/name), invite hash (t.me/+HASH or joinchat),
folder (t.me/addlist/...) -> NOT joinable, must be opened by hand.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

from telethon.errors import FloodWaitError, InviteRequestSentError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)

import database

PUBLIC_RE = re.compile(r"^t\.me/([A-Za-z][A-Za-z0-9_]{3,31})$", re.IGNORECASE)

JOIN_LIMIT_DEFAULT = 10
JOIN_LIMIT_MAX = 50


def clamp_join_limit(n: int) -> int:
    return max(1, min(JOIN_LIMIT_MAX, n))


def prepare_join_items(urls: list[str]) -> tuple[list[tuple[str, str, str]], int]:
    """Classify + filter raw urls. Returns (items, folder_count).

    items: list of (url, kind, payload), folders/unsupported skipped.
    """
    items: list[tuple[str, str, str]] = []
    folders = 0
    for url in urls:
        kind, payload = classify_link(url)
        if kind == "folder":
            folders += 1
            continue
        if kind == "unsupported":
            continue
        items.append((url, kind, payload))
    return items, folders


def classify_link(url: str) -> tuple[str, str]:
    """Return (kind, payload). kinds: public, invite, folder, unsupported."""
    try:
        u = urlparse(url.strip())
    except Exception:
        return "unsupported", url
    host = (u.hostname or "").lower()
    if host not in ("t.me", "telegram.me", "telegram.dog"):
        return "unsupported", url
    path = u.path.strip("/")
    if path.startswith("+"):
        return "invite", path[1:]
    if path.startswith("joinchat/"):
        return "invite", path[len("joinchat/"):]
    if path.startswith("addlist/"):
        return "folder", path[len("addlist/"):]
    m = PUBLIC_RE.match(f"t.me/{path}")
    if m and "/" not in path:
        return "public", m.group(1)
    return "unsupported", url


async def attempt_join(client, kind: str, payload: str) -> str:
    """One join attempt. Returns short Arabic result (no secrets)."""
    try:
        if kind == "public":
            try:
                entity = await client.get_entity(payload)
            except Exception:
                return "مش لاقي اليوزر."
            from telethon.tl.types import Channel, Chat, User
            if isinstance(entity, User):
                return "ده حساب شخص/بوت."
            if isinstance(entity, Chat):
                return "جروب صغير — لازم دعوة."
            try:
                await client(JoinChannelRequest(entity))
                return "done"
            except (UserAlreadyParticipantError, Exception) as e:
                if isinstance(e, UserAlreadyParticipantError) or \
                        "ALREADY_PARTICIPANT" in str(e) or "USER_ALREADY_PARTICIPANT" in str(e):
                    return "عضو أصلًا."
                if isinstance(e, InviteRequestSentError) or "INVITE_REQUEST_SENT" in str(e):
                    return "طلب انضمام مستني موافقة."
                raise
        if kind == "invite":
            try:
                inv = await client(CheckChatInviteRequest(payload))
            except Exception:
                return "الدعوة انتهت."
            already = getattr(inv, "already_participant", False)
            if already:
                return "عضو أصلًا."
            try:
                await client(ImportChatInviteRequest(payload))
                return "done"
            except Exception as e:
                if "FLOOD" in type(e).__name__.upper():
                    raise
                if "INVITE_REQUEST_SENT" in str(e):
                    return "طلب انضمام مستني موافقة."
                return "فشل."
    except FloodWaitError as e:
        raise
    except Exception:
        return "فشل."
    return "فشل."


class JoinQueue:
    """Paced auto-join worker: one attempt per interval, daily cap, flood-stop."""

    def __init__(self, db_path, get_client, notify):
        self.db_path = db_path
        self._get_client = get_client  # async () -> (phone, client)
        self._notify = notify          # async (text) -> None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.items: list[tuple[str, str]] = []
        self.interval_min = 5
        self.phone = ""

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, phone: str, items: list[tuple[str, str]],
              interval_min: int) -> None:
        self.stop()
        self.items = list(items)
        self.interval_min = max(1, min(1440, interval_min))
        self.phone = phone
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._stop.set()
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        ok = fail = 0
        total = len(self.items)
        try:
            phone, client = await self._get_client()
        except Exception as e:
            await self._notify(f"مش قادر أوصل ({e}).")
            return
        await self._notify(f"▶️ بدأت: {total} لينك كل {self.interval_min} دقيقة.")
        results: list[str] = []
        for i, (kind, payload) in enumerate(self.items, 1):
            if self._stop.is_set():
                break
            # Enforce single-number operation: stop if the active number changed.
            try:
                current = database.get_active_phone(self.db_path)
            except Exception:
                current = self.phone
            if current != self.phone:
                await self._notify("⏹ الرقم النشط اتغير — وقفت الانضمام.")
                break
            # No daily cap on joins (user's choice). FloodWait from Telegram
            # still stops the queue. Attempts are still counted for status.
            try:
                res = await attempt_join(client, kind, payload)
            except FloodWaitError as e:
                await self._notify(f"تليجرام طلب انتظار {e.seconds} ث — وقفت.")
                break
            except Exception:
                res = "فشل."
            database.counter_inc(self.db_path, "joins")
            if res == "done":
                ok += 1
                results.append(f"{i}. ✅ انضميت")
            else:
                fail += 1
                label = payload if kind == "public" else (payload[:12] + "…")
                results.append(f"{i}. ❌ {label}: {res}")
            left = total - i
            if left > 0 and not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=self.interval_min * 60)
                    break
                except asyncio.TimeoutError:
                    pass
        report = f"🏁 خلصت: {ok}✅ {fail}❌ من {total}."
        if results:
            report += "\n" + "\n".join(results[:10])
        await self._notify(report)

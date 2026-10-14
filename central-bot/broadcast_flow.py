"""Broadcast a text ad to the active account's groups, one post per interval,
until the list is done. Stop button anytime. Daily cap + flood-stop."""
from __future__ import annotations

import asyncio

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

import database


def describe_send_error(e: Exception) -> str:
    """Short Arabic reason for a failed post (no secrets)."""
    blob = (type(e).__name__ + " " + str(e)).upper().replace("_", "").replace(" ", "")
    if "SLOWMODE" in blob:
        secs = getattr(e, "seconds", "?")
        return f"وضع بطيء ({secs} ث)"
    if "WRITEFORBIDDEN" in blob:
        return "النشر مقفول للأعضاء"
    if "BANNED" in blob:
        return "محظور من النشر"
    if "ADMINR" in blob or "CHATADMINREQUIRED" in blob:
        return "محتاج صلاحية أدمن"
    return type(e).__name__ or "فشل"


def _can_post(ent) -> bool:
    """Broadcast channels need admin post rights; groups are assumed postable
    (real failures still surface per-target at send time)."""
    if isinstance(ent, Chat):
        return True
    if isinstance(ent, Channel):
        if getattr(ent, "megagroup", False):
            return True
        if getattr(ent, "creator", False):
            return True
        rights = getattr(ent, "admin_rights", None)
        if rights and getattr(rights, "post_messages", False):
            return True
        return False
    return False


async def list_post_targets(client) -> list[tuple]:
    """ALL chats: groups + channels. Each item is (entity, can_post)."""
    targets = []
    async for dlg in client.iter_dialogs():
        ent = dlg.entity
        if isinstance(ent, (Chat, Channel)):
            targets.append((ent, _can_post(ent)))
    return targets


class BroadcastWorker:
    def __init__(self, db_path, get_client, notify):
        self.db_path = db_path
        self._get_client = get_client
        self._notify = notify
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.bid = 0

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, phone: str, text: str, interval_min: int,
              targets: list) -> int:
        self.stop()
        self._stop = asyncio.Event()
        bid = database.create_broadcast(self.db_path, phone, text,
                                        max(1, min(1440, interval_min)),
                                        len(targets))
        self.bid = bid
        self._task = asyncio.create_task(
            self._run(bid, phone, text, max(1, min(1440, interval_min)), targets))
        return bid

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._stop.set()
            self._task.cancel()
        self._task = None

    async def _run(self, bid: int, phone: str, text: str,
                   interval_min: int, targets: list) -> None:
        try:
            _, client = await self._get_client()
        except Exception as e:
            database.update_broadcast(self.db_path, bid, status="error")
            await self._notify(f"مش قادر أوصل ({e}).")
            return
        cap = int(database.kv_get(self.db_path, "post_cap", "100") or "100")
        ok = fail = skip = 0
        total = len(targets)
        results: list[str] = []
        await self._notify(f"📢 بدأت: {total} شات كل {interval_min} دقيقة.")
        for i, (ent, can_post) in enumerate(targets, 1):
            if self._stop.is_set():
                break
            # Enforce single-number operation: stop if the active number changed.
            try:
                current = database.get_active_phone(self.db_path)
            except Exception:
                current = phone
            if current != phone:
                database.update_broadcast(self.db_path, bid, done_ok=ok,
                                          done_fail=fail, status="stopped")
                await self._notify("⏹ الرقم النشط اتغير — وقفت النشر.")
                return
            if database.counter_get(self.db_path, "posts") >= cap:
                await self._notify(f"وصلت للحد اليومي ({cap}).")
                break
            title = (getattr(ent, "title", "") or "?")[:30]
            if not can_post:
                skip += 1
                results.append(f"{i}. ⏭️ {title}: مش أدمن (تخطي)")
                continue
            try:
                await client.send_message(ent, text)
                ok += 1
                results.append(f"{i}. ✅ {title}")
            except FloodWaitError as e:
                database.update_broadcast(self.db_path, bid, done_ok=ok,
                                          done_fail=fail, status="flood")
                await self._notify(f"تليجرام طلب انتظار {e.seconds} ث — وقفت عند {title}.")
                return
            except Exception as e:
                fail += 1
                results.append(f"{i}. ❌ {title}: {describe_send_error(e)}")
            database.counter_inc(self.db_path, "posts")
            database.update_broadcast(self.db_path, bid, done_ok=ok, done_fail=fail)
            if i < total and not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=interval_min * 60)
                    break
                except asyncio.TimeoutError:
                    pass
        status = "stopped" if self._stop.is_set() else "done"
        database.update_broadcast(self.db_path, bid, status=status,
                                  done_ok=ok, done_fail=fail)
        report = f"🏁 النشر خلص: {ok}✅ {fail}❌ {skip}⏭️ من {total}."
        if results:
            report += "\n" + "\n".join(results[:12])
        await self._notify(report)

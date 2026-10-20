"""Broadcast a text ad to the active account's groups, one post per interval,
until the list is done. Stop button anytime. Daily cap + flood-stop."""
from __future__ import annotations

import asyncio

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

import database
from helpers import format_duration_ar


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
                database.counter_inc(self.db_path, f"sent:{phone}")
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


class SuperBroadcastWorker:
    """Post the same ad from several accounts, each paced independently."""

    def __init__(self, db_path, sm, notify):
        self.db_path = db_path
        self._sm = sm
        self._notify = notify
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.phones: list[str] = []
        self.text = ""
        self.interval_sec = 60
        self.daily_limit = 0

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, phones: list[str], text: str, interval_sec: int,
              daily_limit: int) -> None:
        self.stop()
        self.phones = list(dict.fromkeys(phones))
        self.text = text
        self.interval_sec = max(10, min(24 * 60 * 60, int(interval_sec)))
        self.daily_limit = max(0, int(daily_limit))
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._stop.set()
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        limit_txt = f"{self.daily_limit} لكل رقم" if self.daily_limit else "بدون حد"
        await self._notify(
            f"🚀 السوبر بدأ: {len(self.phones)} رقم — كل "
            f"{format_duration_ar(self.interval_sec)} — الحد اليومي: {limit_txt}.")
        results = await asyncio.gather(
            *(self._run_account(p) for p in self.phones), return_exceptions=True)
        ok = fail = skip = 0
        for r in results:
            if isinstance(r, tuple) and len(r) == 5:
                ok += r[1]
                fail += r[2]
                skip += r[3]
            elif isinstance(r, Exception):
                await self._notify(f"❌ حساب وقف بخطأ: {type(r).__name__}.")
        head = "⏹ السوبر اتوقف" if self._stop.is_set() else "🏁 السوبر خلص"
        await self._notify(f"{head}: {ok}✅ {fail}❌ {skip}⏭️.")

    async def _run_account(self, phone: str) -> tuple:
        acc = database.get_account(self.db_path, phone) or {}
        name = acc.get("display_name") or phone
        sent = database.counter_get(self.db_path, f"sent:{phone}")
        if self.daily_limit and sent >= self.daily_limit:
            await self._notify(
                f"⏭️ {name}: وصل للحد اليومي ({self.daily_limit}) — تخطي.")
            return (phone, 0, 0, 0, "limit")
        ok = fail = skip = 0
        failures: list[str] = []
        total = 0
        try:
            async with self._sm.temp_client(phone) as client:
                targets = await asyncio.wait_for(
                    list_post_targets(client), timeout=300)
                total = len(targets)
                for i, (ent, can_post) in enumerate(targets, 1):
                    if self._stop.is_set():
                        break
                    if not can_post:
                        skip += 1
                        continue
                    if self.daily_limit and sent >= self.daily_limit:
                        await self._notify(
                            f"🛑 {name}: وصل للحد اليومي ({self.daily_limit}).")
                        break
                    title = (getattr(ent, "title", "") or "?")[:30]
                    try:
                        await client.send_message(ent, self.text)
                        ok += 1
                        sent += 1
                        database.counter_inc(self.db_path, f"sent:{phone}")
                        database.counter_inc(self.db_path, "posts")
                    except FloodWaitError as e:
                        fail += 1
                        database.counter_inc(self.db_path, "posts")
                        await self._notify(
                            f"⏳ {name}: تليجرام طلب انتظار {e.seconds} ث — وقفت الرقم ده.")
                        await self._notify(
                            self._report(name, ok, fail, skip, total, failures, True))
                        return (phone, ok, fail, skip, "flood")
                    except Exception as e:
                        fail += 1
                        database.counter_inc(self.db_path, "posts")
                        if len(failures) < 5:
                            failures.append(f"{title}: {describe_send_error(e)}")
                    if i < total and not self._stop.is_set():
                        try:
                            await asyncio.wait_for(
                                self._stop.wait(), timeout=self.interval_sec)
                        except asyncio.TimeoutError:
                            pass
                await self._notify(
                    self._report(name, ok, fail, skip, total, failures))
                return (phone, ok, fail, skip, "")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = str(e).replace("\n", " ")[:120]
            detail = f"{type(e).__name__}: {reason}" if reason else type(e).__name__
            await self._notify(f"❌ {name}: وقف — {detail}")
            return (phone, ok, fail, skip, "error")

    @staticmethod
    def _report(name, ok, fail, skip, total, failures, stopped=False) -> str:
        head = "⏹" if stopped else "🏁"
        text = f"{head} {name}: {ok}✅ {fail}❌ {skip}⏭️ من {total}."
        if failures:
            text += "\n" + "\n".join(failures)
        return text

"""Broadcast a text ad to the active account's groups, one post per interval,
until the list is done. Stop button anytime. Daily cap + flood-stop."""
from __future__ import annotations

import asyncio
import json
import uuid

from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

import database
from helpers import chunk_text


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
    """Post the same ad from several accounts, in round-robin batches.

    Each account sends batch_size successful posts, then the next account
    sends its batch, and so on until every dialog has been processed once.
    Progress is persisted in SQLite so an interrupted run can resume.
    """

    def __init__(self, db_path, sm, notify):
        self.db_path = db_path
        self._sm = sm
        self._notify = notify
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._state: dict | None = None
        self._finalized = False
        self.phones: list[str] = []
        self.text = ""
        self.duration_sec = 60
        self.batch_size = 0
        self.min_post_gap_sec = 10

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, phones: list[str], text: str, duration_sec: int,
              batch_size: int) -> None:
        self.stop()
        self.phones = list(dict.fromkeys(phones))
        self.text = text
        self.duration_sec = max(10, min(24 * 60 * 60, int(duration_sec)))
        self.batch_size = max(0, min(100000, int(batch_size)))
        self._stop = asyncio.Event()
        self._finalized = False

        job = self._load_job()
        if (job is not None and job.get("status") == "running"
                and job.get("phones") == self.phones
                and job.get("text") == self.text
                and int(job.get("duration_sec", 0)) == self.duration_sec
                and int(job.get("batch_size", -1)) == self.batch_size):
            self._state = job
        else:
            self._state = {
                "id": uuid.uuid4().hex,
                "status": "running",
                "phones": list(self.phones),
                "text": self.text,
                "duration_sec": self.duration_sec,
                "batch_size": self.batch_size,
                "accounts": {
                    phone: {
                        "ok": 0,
                        "fail": 0,
                        "skip": 0,
                        "total": 0,
                        "processed": [],
                        "blocked": [],
                        "status": "running",
                        "note": "",
                    }
                    for phone in self.phones
                },
            }
        self._ensure_accounts()
        self._save_job()
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._stop.set()
            self._task.cancel()
        self._task = None
        if self._state is not None:
            self._state["status"] = "stopped"
            try:
                self._save_job()
            except Exception:
                pass

    def _load_job(self) -> dict | None:
        try:
            raw = database.kv_get(self.db_path, "super_job", "")
            if not raw:
                return None
            job = json.loads(raw)
            return job if isinstance(job, dict) else None
        except Exception:
            return None

    def _ensure_accounts(self) -> None:
        if self._state is None:
            return
        accounts = self._state.setdefault("accounts", {})
        for phone in self.phones:
            acc = accounts.setdefault(phone, {
                "ok": 0,
                "fail": 0,
                "skip": 0,
                "total": 0,
                "processed": [],
                "blocked": [],
                "status": "running",
                "note": "",
            })
            acc.setdefault("processed", [])
            acc.setdefault("blocked", [])
            for key in ("ok", "fail", "skip", "total"):
                acc.setdefault(key, 0)

    def _save_job(self) -> None:
        if self._state is not None:
            database.kv_set(
                self.db_path, "super_job",
                json.dumps(self._state, ensure_ascii=False))

    def _account_state(self, phone: str) -> dict:
        self._ensure_accounts()
        assert self._state is not None
        return self._state["accounts"][phone]

    def _entity_id(self, ent) -> int:
        try:
            return int(getattr(ent, "id", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _batch_gap(self, remaining_count: int) -> float:
        if self.batch_size > 0:
            planned = min(self.batch_size, max(1, remaining_count))
        else:
            planned = max(1, remaining_count)
        planned = max(1, planned)
        return max(float(self.min_post_gap_sec),
                   float(self.duration_sec) / planned)

    async def _run(self) -> None:
        self._finalized = False
        stopped = False
        try:
            await self._run_body()
        except asyncio.CancelledError:
            stopped = True
        except Exception as e:
            if self._state is not None:
                for phone in self.phones:
                    acc = self._account_state(phone)
                    if acc.get("status") == "running":
                        acc["status"] = "error"
                        acc["note"] = f"❌ وقف بسبب {type(e).__name__}"
            stopped = False
        finally:
            if not self._finalized:
                await self._finalize(stopped or self._stop.is_set())

    async def _run_body(self) -> None:
        assert self._state is not None
        targets_by_phone: dict[str, list] = {}

        for phone in self.phones:
            if self._stop.is_set():
                return
            acc = self._account_state(phone)
            try:
                async with self._sm.temp_client(phone) as client:
                    targets = await asyncio.wait_for(
                        list_post_targets(client), timeout=300)
            except Exception as e:
                acc["status"] = "error"
                reason = str(e).replace("\n", " ")[:120]
                acc["note"] = (f"❌ وقف بسبب {type(e).__name__}: {reason}"
                               if reason else f"❌ وقف بسبب {type(e).__name__}")
                targets_by_phone[phone] = []
                self._save_job()
                continue
            targets_by_phone[phone] = targets
            acc["total"] = len(targets)
            if acc.get("status") not in ("flood", "error"):
                acc["status"] = "running"
            self._save_job()

        remaining: dict[str, list] = {}
        for phone in self.phones:
            acc = self._account_state(phone)
            processed = set(acc.get("processed", []))
            remaining[phone] = [
                item for item in targets_by_phone.get(phone, [])
                if self._entity_id(item[0]) not in processed
            ]

        while (not self._stop.is_set()
               and any(remaining.get(p) for p in self.phones)):
            progressed = False
            for phone in self.phones:
                if self._stop.is_set():
                    break
                if not remaining.get(phone):
                    continue
                acc = self._account_state(phone)
                if acc.get("status") == "flood":
                    remaining[phone] = []
                    continue

                progressed = True
                acc["status"] = "running"
                postable_remaining = [
                    item for item in remaining[phone]
                    if item[1]
                    and self._entity_id(item[0]) not in set(acc.get("processed", []))
                ]
                if self.batch_size > 0:
                    batch_limit = self.batch_size
                else:
                    batch_limit = len(postable_remaining)
                batch_limit = max(1, batch_limit) if postable_remaining else 0
                gap = self._batch_gap(len(postable_remaining))

                successes = 0
                flooded = False
                try:
                    async with self._sm.temp_client(phone) as client:
                        while (remaining[phone]
                               and not self._stop.is_set()
                               and (batch_limit == 0 or successes < batch_limit)):
                            item = remaining[phone].pop(0)
                            ent, can_post = item
                            eid = self._entity_id(ent)
                            if eid in set(acc.get("processed", [])):
                                continue
                            title = (getattr(ent, "title", "") or "?").strip()[:80] or "?"

                            if not can_post:
                                acc["skip"] += 1
                                acc["processed"].append(eid)
                                self._save_job()
                                continue

                            try:
                                await client.send_message(ent, self.text)
                                acc["ok"] += 1
                                successes += 1
                                acc["processed"].append(eid)
                                database.counter_inc(self.db_path, f"sent:{phone}")
                                database.counter_inc(self.db_path, "posts")
                            except FloodWaitError as e:
                                acc["fail"] += 1
                                acc["processed"].append(eid)
                                acc["blocked"].append(
                                    f"{title} — تليجرام طلب انتظار {e.seconds} ث")
                                acc["status"] = "flood"
                                acc["note"] = (f"⏳ تليجرام طلب انتظار {e.seconds} ث "
                                               "— وقف الحساب.")
                                flooded = True
                                database.counter_inc(self.db_path, "posts")
                                self._save_job()
                                break
                            except Exception as e:
                                acc["fail"] += 1
                                acc["processed"].append(eid)
                                acc["blocked"].append(
                                    f"{title} — {describe_send_error(e)}")
                                database.counter_inc(self.db_path, "posts")
                            self._save_job()

                            if not flooded and not self._stop.is_set():
                                try:
                                    await asyncio.wait_for(
                                        self._stop.wait(), timeout=gap)
                                except asyncio.TimeoutError:
                                    pass
                except Exception as e:
                    acc["status"] = "error"
                    reason = str(e).replace("\n", " ")[:120]
                    acc["note"] = (f"❌ وقف بسبب {type(e).__name__}: {reason}"
                                   if reason else f"❌ وقف بسبب {type(e).__name__}")
                    remaining[phone] = []
                    self._save_job()

                if flooded:
                    remaining[phone] = []
                    self._save_job()

            if not progressed:
                break

    async def _finalize(self, stopped: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._state is None:
            return

        total_ok = total_fail = total_skip = 0
        parts = []
        head = "⏹ السوبر اتوقف" if stopped else "🏁 السوبر خلص"
        for phone in self.phones:
            acc = self._account_state(phone)
            total_ok += int(acc.get("ok", 0))
            total_fail += int(acc.get("fail", 0))
            total_skip += int(acc.get("skip", 0))
            if stopped and acc.get("status") == "running":
                acc["status"] = "stopped"
                if not acc.get("note"):
                    acc["note"] = "⏹ اتوقف يدويًا."
            if not stopped and acc.get("status") == "running":
                acc["status"] = "done"
        parts.append(f"{head}: {total_ok}✅ {total_fail}❌ {total_skip}⏭️.")
        parts.append("")

        for phone in self.phones:
            acc = self._account_state(phone)
            name = (database.get_account(self.db_path, phone) or {}).get(
                "display_name") or phone
            failed = list(acc.get("blocked", []))
            note = acc.get("note", "")
            parts.append(self._report(
                name, int(acc.get("ok", 0)), int(acc.get("fail", 0)),
                int(acc.get("skip", 0)), int(acc.get("total", 0)), failed, note))
            parts.append("")

        self._state["status"] = "stopped" if stopped else "done"
        try:
            self._save_job()
        except Exception:
            pass

        text = "\n".join(parts).strip()
        for chunk in chunk_text(text, 3600):
            await self._notify(chunk)

    async def _send_report(self, name: str, text: str) -> None:
        chunks = chunk_text(text, 3600)
        if len(chunks) == 1:
            await self._notify(chunks[0])
            return
        for i, chunk in enumerate(chunks, 1):
            await self._notify(
                f"📄 تقرير {name} ({i}/{len(chunks)}):\n{chunk}")

    @staticmethod
    def _report(name, ok, fail, skip, total, failed, note="") -> str:
        lines = [
            f"🏁 تقرير {name}:",
            f"✅ تم النشر بنجاح: {ok} من {total}.",
        ]
        if failed:
            lines.append(f"\n🚫 جروبات خد منها حظر/منع ({len(failed)}):")
            lines.extend(f"• {t}" for t in failed)
        if skip:
            lines.append(f"\n⏭️ متخطى: {skip} (مش متاح للنشر).")
        if note:
            lines.append(f"\n{note}")
        return "\n".join(lines)

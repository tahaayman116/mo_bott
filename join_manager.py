"""Join queue (Phase 2b): reviewed queue + optional paced auto-execution.

Flow:
  !tg join <url...> -> validate + read-only preview each -> pending requests
  !tg pending       -> list requests awaiting decision
  !tg confirm_join <id> -> ONE manual join attempt (cooldown + flood checks)
  !tg cancel_join <id>  -> cancel a pending request
  !tg auto on/off   -> automatic paced processing of the REVIEWED queue
  !tg interval <min>-> change pacing between automatic joins

Rules:
- Collecting a link NEVER creates a join request, and auto mode NEVER
  touches collected links directly: only requests you explicitly queued
  via !tg join are eligible. Blind-joining unknown groups is how
  accounts get flagged (spam/scam traps), so review stays mandatory.
- Preview refusals (user/bot/expired/invalid) create NO request.
- Expired/invalid/denied are final: never auto-retried.
- Network failure mid-join -> 'failed_uncertain' (verify manually, no blind retry).
- Flood/slow -> back to re-confirmable state, server wait honored; auto stops.
- One join attempt per confirm/auto-step; pacing = max(cooldown, interval).
- Daily cap on attempts (manual + auto combined).
- All Telethon names used here verified against installed 1.45.0.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telethon.errors import FloodWaitError, SlowModeWaitError
from telethon.errors.rpcerrorlist import (
    ChannelPrivateError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
    UsersTooMuchError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.chatlists import (
    CheckChatlistInviteRequest,
    JoinChatlistInviteRequest,
)
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import PeerChannel, PeerChat, PeerUser
from telethon.tl.types.chatlists import ChatlistInvite, ChatlistInviteAlready

import database
from database import (
    JR_ALREADY,
    JR_CANCELLED,
    JR_DENIED,
    JR_EXPIRED,
    JR_FLOOD_DEFERRED,
    JR_INVALID,
    JR_JOINED,
    JR_JOINING,
    JR_PENDING,
    JR_REQUESTED,
    JR_UNCERTAIN,
)
from link_parser import (
    extract_folder_slug,
    extract_invite_hash,
    extract_username,
    parse_message_text,
)
from link_verifier import (
    DEST_ALREADY_MEMBER,
    DEST_BASIC_GROUP,
    DEST_BOT,
    DEST_CHANNEL,
    DEST_EXPIRED,
    DEST_FOLDER,
    DEST_INVALID,
    DEST_PRIVATE_INVITE,
    DEST_SUPERGROUP,
    DEST_USER,
    preview_folder,
    preview_invite,
    preview_public,
)

LAST_JOIN_TS_KEY = "last_join_ts"
JOIN_DAY_KEY = "join_day"
JOIN_DAY_COUNT_KEY = "join_day_count"
AUTO_ENABLED_KEY = "autojoin_enabled"
AUTO_INTERVAL_KEY = "autojoin_interval_min"
AUTO_MAXDAY_KEY = "autojoin_max_per_day"

RECONFIRMABLE = frozenset({JR_PENDING, JR_FLOOD_DEFERRED})

JOINABLE_PREVIEW = frozenset({
    DEST_BASIC_GROUP, DEST_SUPERGROUP, DEST_CHANNEL, DEST_PRIVATE_INVITE,
    DEST_FOLDER,
})

_GROUP_CANDIDATE_KINDS = ("private_invite", "public_username", "folder")

BULK_MAX_LINKS = 10          # max links accepted per !tg join message
BULK_PREVIEW_GAP = 3         # seconds between previews in one bulk message
AUTO_DEFAULT_INTERVAL = 30   # minutes between automatic joins
AUTO_DEFAULT_MAXDAY = 20     # max join attempts per UTC day (manual+auto)
AUTO_MIN_INTERVAL = 1        # hard floor: user controls pacing from 1 minute
AUTO_MAX_INTERVAL = 1440     # 24h ceiling
AUTO_RISKY_BELOW = 5         # below this: warn strongly (high restriction risk)


def _cooldown_remaining(db_path: Path, cooldown_seconds: int) -> int:
    if cooldown_seconds <= 0:
        return 0
    raw = database.get_setting(db_path, LAST_JOIN_TS_KEY)
    if not raw:
        return 0
    try:
        elapsed = time.time() - float(raw)
    except ValueError:
        return 0
    left = int(cooldown_seconds - elapsed)
    return left if left > 0 else 0


def _stamp_attempt(db_path: Path, n: int = 1) -> None:
    now = time.time()
    database.set_setting(db_path, LAST_JOIN_TS_KEY, str(now))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if database.get_setting(db_path, JOIN_DAY_KEY) != today:
        database.set_setting(db_path, JOIN_DAY_KEY, today)
        database.set_setting(db_path, JOIN_DAY_COUNT_KEY, str(n))
    else:
        try:
            cur = int(database.get_setting(db_path, JOIN_DAY_COUNT_KEY) or "0")
        except ValueError:
            cur = 0
        database.set_setting(db_path, JOIN_DAY_COUNT_KEY, str(cur + n))


def _today_count(db_path: Path) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if database.get_setting(db_path, JOIN_DAY_KEY) != today:
        return 0
    try:
        return int(database.get_setting(db_path, JOIN_DAY_COUNT_KEY) or "0")
    except ValueError:
        return 0


def _max_per_day(db_path: Path, override: int | None = None) -> int:
    if override is not None:
        return override
    try:
        return int(database.get_setting(db_path, AUTO_MAXDAY_KEY) or str(AUTO_DEFAULT_MAXDAY))
    except ValueError:
        return AUTO_DEFAULT_MAXDAY


def get_auto_settings(db_path: Path) -> dict[str, Any]:
    try:
        interval = int(database.get_setting(db_path, AUTO_INTERVAL_KEY) or str(AUTO_DEFAULT_INTERVAL))
    except ValueError:
        interval = AUTO_DEFAULT_INTERVAL
    interval = min(max(interval, AUTO_MIN_INTERVAL), AUTO_MAX_INTERVAL)
    return {
        "enabled": (database.get_setting(db_path, AUTO_ENABLED_KEY) or "0") == "1",
        "autocollect": is_autocollect_enabled(db_path),
        "interval_min": interval,
        "max_per_day": _max_per_day(db_path),
        "today_count": _today_count(db_path),
        "pending": database.count_pending_joins(db_path),
    }


def set_auto_enabled(db_path: Path, enabled: bool) -> None:
    database.set_setting(db_path, AUTO_ENABLED_KEY, "1" if enabled else "0")
    database.log_audit(db_path, "autojoin", f"auto {'ON' if enabled else 'OFF'}")


def set_interval_minutes(db_path: Path, minutes: int) -> tuple[bool, str]:
    if minutes < AUTO_MIN_INTERVAL or minutes > AUTO_MAX_INTERVAL:
        return False, (f"الفاصل لازم يكون بين {AUTO_MIN_INTERVAL} و {AUTO_MAX_INTERVAL} دقيقة. "
                       f"(القيمة الحالية متغيرتش)")
    database.set_setting(db_path, AUTO_INTERVAL_KEY, str(minutes))
    database.log_audit(db_path, "autojoin", f"interval set to {minutes} min")
    msg = f"✅ الفاصل بقى {minutes} دقيقة."
    if minutes < AUTO_RISKY_BELOW:
        msg += "\n⚠️ تحذير: أقل من 5 خطر عالي — راقب الحالة."
    return True, msg


def set_max_per_day(db_path: Path, value: int) -> tuple[bool, str]:
    if value < 1 or value > 100:
        return False, "الحد اليومي لازم يكون بين 1 و 100."
    database.set_setting(db_path, AUTO_MAXDAY_KEY, str(value))
    return True, f"✅ الحد بقى {value} في اليوم."


AUTOCOLLECT_KEY = "autocollect_enabled"


def is_autocollect_enabled(db_path: Path) -> bool:
    try:
        return (database.get_setting(db_path, AUTOCOLLECT_KEY) or "0") == "1"
    except Exception:
        return False


def set_autocollect(db_path: Path, enabled: bool) -> None:
    database.set_setting(db_path, AUTOCOLLECT_KEY, "1" if enabled else "0")
    database.log_audit(db_path, "autocollect", f"autocollect {'ON' if enabled else 'OFF'}")


async def auto_enqueue_new(db_path: Path, client: Any, safety: Any,
                           parsed: list[Any]) -> tuple[int, int]:
    """Enqueue freshly collected links (opt-in autocollect).

    Only group candidates, capped per message. Returns (created, skipped).
    Caller must ensure live mode; dry-run never calls this.
    """
    cands = [p for p in parsed if p.kind in _GROUP_CANDIDATE_KINDS]
    seen: dict[str, Any] = {}
    for p in cands:
        seen.setdefault(p.normalized_url, p)
    cands = list(seen.values())[:BULK_MAX_LINKS]
    if not cands:
        return 0, 0
    created, skipped, _ = await _preview_and_enqueue(db_path, client, safety, cands)
    if created:
        database.log_audit(db_path, "autocollect", f"auto-enqueued {len(created)} link(s)")
    return len(created), len(skipped)


async def _preview_and_enqueue(db_path: Path, client: Any, safety: Any,
                               cands: list[Any]) -> tuple[list[str], list[str], str]:
    """Preview each candidate (read-only) and queue joinables. Shared core.

    Returns (created_lines, skipped_lines, stopped_early_note).
    Never joins. Stops the batch on server-requested waits.
    """
    created: list[str] = []
    skipped: list[str] = []
    stopped_early = ""
    for i, link in enumerate(cands):
        if i > 0:
            await asyncio.sleep(BULK_PREVIEW_GAP)
        try:
            if link.kind == "folder":
                s = extract_folder_slug(link.normalized_url)
                if not s:
                    skipped.append(f"{link.normalized_url[:60]}: سلاج ناقص.")
                    continue
                preview = await preview_folder(client, s)
            elif link.kind == "private_invite":
                h = extract_invite_hash(link.normalized_url)
                if not h:
                    skipped.append(f"{link.normalized_url[:60]}: هاش ناقص.")
                    continue
                preview = await preview_invite(client, h)
            else:
                u = extract_username(link.normalized_url)
                if not u:
                    skipped.append(f"{link.normalized_url[:60]}: يوزر غلط.")
                    continue
                preview = await preview_public(client, u)
        except (FloodWaitError, SlowModeWaitError) as e:
            decision = safety.handle_error(e)
            secs = decision.get("wait_seconds", 0)
            stopped_early = (f"⏳ تليجرام طلب انتظار {secs} ثانية — استنى وكمل.")
            break
        if preview.dest_type in (DEST_USER, DEST_BOT, DEST_EXPIRED,
                                 DEST_INVALID, DEST_ALREADY_MEMBER):
            skipped.append(f"{link.normalized_url[:60]}: {preview.detail[:90]}")
            continue
        if preview.dest_type not in JOINABLE_PREVIEW:
            skipped.append(f"{link.normalized_url[:60]}: {preview.detail[:90]}")
            continue
        # Same URL already queued and still re-confirmable? Don't duplicate it.
        existing = [r for r in database.list_join_requests(db_path, limit=200)
                    if r["normalized_url"] == link.normalized_url
                    and r["status"] in RECONFIRMABLE]
        if existing:
            skipped.append(f"{link.normalized_url[:60]}: مطلوب بالفعل (#{existing[0]['id']}).")
            continue
        req = database.create_join_request(
            db_path, link.normalized_url, link.kind,
            preview.title, preview.dest_type, preview.detail)
        database.log_audit(db_path, "join_pending",
                           f"request {req['id']} created for {link.normalized_url[:80]}")
        created.append(f"#{req['id']} | {(preview.title or link.normalized_url)[:40]} | {preview.dest_type}")
    return created, skipped, stopped_early


async def handle_join_command(db_path: Path, client: Any, safety: Any,
                              raw_url: str) -> str:
    """Validate + preview URLs (up to BULK_MAX_LINKS). Pending requests only if joinable."""
    parsed = parse_message_text(raw_url)
    cands = [p for p in parsed if p.kind in _GROUP_CANDIDATE_KINDS]
    if not cands:
        return ("مفيش لينك صالح.\n"
                "مثال: انضم https://t.me/example_group")
    if len(cands) > BULK_MAX_LINKS:
        return (f"لقيت {len(cands)} لينك — الحد {BULK_MAX_LINKS} في الرسالة.\n"
                f"قسّمهم على كذا رسالة.")
    # Deduplicate within the message, keep order.
    seen: dict[str, Any] = {}
    for p in cands:
        seen.setdefault(p.normalized_url, p)
    cands = list(seen.values())
    created, skipped, stopped_early = await _preview_and_enqueue(
        db_path, client, safety, cands)
    lines: list[str] = []
    if created:
        lines.append("🔍 معاينة (محدش انضم):")
        lines.extend(created)
        lines.append("")
        lines.append("أكد: أكد <رقم> — الغي: الغي <رقم>")
        lines.append("تلقائي: تلقائي شغل")
    if skipped:
        lines.append("")
        lines.append("⏭️ اتخطى:")
        lines.extend(skipped[:5])
    if stopped_early:
        lines.append("")
        lines.append(stopped_early)
    if not created and not skipped and not stopped_early:
        return "مفيش لينك صالح."
    return "\n".join(lines)


def handle_pending(db_path: Path) -> str:
    rows = database.list_join_requests(db_path, limit=20)
    pend = [r for r in rows if r["status"] in RECONFIRMABLE]
    if not pend:
        return "مفيش طلبات معلقة."
    lines = ["⏳ المعلقة:"]
    for r in pend[:10]:
        note = ""
        if r["status"] == JR_FLOOD_DEFERRED and r["error"]:
            note = f" — {r['error'][:60]}"
        lines.append(
            f"#{r['id']} | {(r['preview_title'] or r['normalized_url'])[:40]} "
            f"| {r['preview_type']}{note}")
    lines.append("أكد: أكد <رقم> — الغي: الغي <رقم>")
    return "\n".join(lines)


async def handle_confirm(db_path: Path, client: Any, safety: Any,
                         request_id: int, cooldown_seconds: int = 60,
                         max_per_day: int | None = None, live: bool = True) -> str:
    req = database.get_join_request(db_path, request_id)
    if req is None:
        return f"مفيش طلب بالرقم ده."
    if req["status"] not in RECONFIRMABLE:
        return (f"الطلب #{request_id} خلص ({req['status']}).\n"
                f"مينفعش تأكيد تاني.")
    if not live:
        return "وضع التجربة شغال — الانضمام مقفول."
    if getattr(safety, "emergency_stop", False) or getattr(safety, "joining_paused", False):
        return "الانضمام موقوف — شوف حالة."
    wait = 0.0
    try:
        wait = max(float(safety.flood_wait_remaining()),
                   float(safety.slow_wait_remaining()))
    except Exception:
        wait = 0.0
    if wait > 0:
        return f"استنى {int(wait)} ثانية وأكد تاني."
    left = _cooldown_remaining(db_path, cooldown_seconds)
    if left > 0:
        return f"استنى {left} ثانية وأكد تاني."
    cap = _max_per_day(db_path, max_per_day)
    used = _today_count(db_path)
    if used >= cap:
        return f"وصلت للحد اليومي ({cap})."
    if req["kind"] == "folder":
        # Folder join = several groups at once; accounted per chat below.
        return await _confirm_folder(db_path, client, safety, req, request_id, cap, used)
    _stamp_attempt(db_path)
    database.set_join_status(db_path, request_id, JR_JOINING, bump_attempts=True)
    try:
        if req["kind"] == "private_invite":
            h = extract_invite_hash(req["normalized_url"])
            if not h:
                database.set_join_status(db_path, request_id, JR_INVALID,
                                         result="empty hash", error="hash missing")
                return "اللينك بايظ — اتقفل."
            await client(ImportChatInviteRequest(h))
        else:
            u = extract_username(req["normalized_url"])
            if not u:
                database.set_join_status(db_path, request_id, JR_INVALID,
                                         result="bad username", error="username missing")
                return "اللينك بايظ — اتقفل."
            await client(JoinChannelRequest(u))
    except (FloodWaitError, SlowModeWaitError) as e:
        decision = safety.handle_error(e)
        secs = decision.get("wait_seconds", 0)
        database.set_join_status(
            db_path, request_id, JR_FLOOD_DEFERRED,
            result="deferred", error=f"server wait {secs}s")
        return f"⏳ انتظار {secs} ثانية — استنى وأكد تاني."
    except InviteRequestSentError:
        database.set_join_status(db_path, request_id, JR_REQUESTED,
                                 result="join request sent")
        database.log_audit(db_path, "join_requested", f"request {request_id}")
        return "📩 طلب اتبعت — مستني الأدمن."
    except UserAlreadyParticipantError:
        database.set_join_status(db_path, request_id, JR_ALREADY,
                                 result="already a member")
        return "عضو أصلًا."
    except InviteHashExpiredError:
        database.set_join_status(db_path, request_id, JR_EXPIRED,
                                 result="expired", error="invite expired")
        return "الدعوة انتهت."
    except InviteHashInvalidError:
        database.set_join_status(db_path, request_id, JR_INVALID,
                                 result="invalid", error="invite invalid")
        return "الدعوة غير صالحة."
    except (UsersTooMuchError, ChannelPrivateError) as e:
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="denied", error=type(e).__name__)
        reason = ("وصلت للحد الأقصى للحساب."
                  if isinstance(e, UsersTooMuchError)
                  else "خاص ومش متاح.")
        return f"مرفوض: {reason}"
    except (OSError, TimeoutError) as e:
        # Note: asyncio.TimeoutError is builtin TimeoutError since Python 3.11.
        safety.handle_error(e)
        database.set_join_status(db_path, request_id, JR_UNCERTAIN,
                                 result="uncertain",
                                 error=f"network failure mid-join ({type(e).__name__})")
        return "⚠️ عطل شبكة — النتيجة مش مؤكدة. اتأكد بنفسك."
    except Exception as e:
        safety.handle_error(e)
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="failed",
                                 error=f"{type(e).__name__}: {e}"[:200])
        return f"فشل ({type(e).__name__})."
    database.set_join_status(db_path, request_id, JR_JOINED, result="joined")
    database.log_audit(db_path, "join_success", f"request {request_id} joined")
    safety.record_success(f"joined request {request_id}")
    title = req.get("preview_title") or req["normalized_url"]
    return f"✅ انضميت: {title}"


async def _confirm_folder(db_path: Path, client: Any, safety: Any,
                          req: dict[str, Any], request_id: int,
                          cap: int, used: int) -> str:
    """Join every missing chat inside a folder invite at once."""
    slug = extract_folder_slug(req["normalized_url"])
    if not slug:
        database.set_join_status(db_path, request_id, JR_INVALID,
                                 result="empty slug", error="slug missing")
        return "اللينك بايظ — اتقفل."
    try:
        res = await client(CheckChatlistInviteRequest(slug))
    except (FloodWaitError, SlowModeWaitError) as e:
        decision = safety.handle_error(e)
        secs = decision.get("wait_seconds", 0)
        database.set_join_status(db_path, request_id, JR_FLOOD_DEFERRED,
                                 result="deferred", error=f"server wait {secs}s")
        return f"⏳ انتظار {secs} ثانية — استنى وأكد تاني."
    except (InviteHashExpiredError, InviteHashInvalidError):
        database.set_join_status(db_path, request_id, JR_INVALID,
                                 result="invalid", error="folder invite invalid")
        return "الفولدر غير صالح."
    except Exception as e:
        safety.handle_error(e)
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="failed", error=f"{type(e).__name__}: {e}"[:200])
        return f"المعاينة فشلت ({type(e).__name__})."
    if isinstance(res, ChatlistInviteAlready):
        missing = [p for p in (getattr(res, "missing_peers", None) or [])
                   if isinstance(p, (PeerChannel, PeerChat))]
        if not missing:
            database.set_join_status(db_path, request_id, JR_ALREADY,
                                     result="already a member")
            return "كله عندك أصلًا."
        peers = missing
    elif isinstance(res, ChatlistInvite):
        peers = [p for p in (getattr(res, "peers", None) or [])
                 if isinstance(p, (PeerChannel, PeerChat))]
        if not peers:
            database.set_join_status(db_path, request_id, JR_JOINED,
                                     result="empty folder - nothing to join")
            return "الفولدر فاضي."
    else:
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="failed", error="unexpected check response")
        return "رد غريب — اتقفل."
    n = len(peers)
    if used + n > cap:
        return (f"الفولدر فيه {n} — هيعدي الحد اليومي ({cap}).\n"
                f"علّيه: حد 50 أو استنى بكرة.")
    _stamp_attempt(db_path, n)
    database.set_join_status(db_path, request_id, JR_JOINING, bump_attempts=True)
    input_peers = []
    for p in peers:
        try:
            input_peers.append(await client.get_input_entity(p))
        except Exception:
            continue
    if not input_peers:
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="failed", error="could not resolve peers")
        return "مقدرتش أوصل للجروبات — اتقفل."
    try:
        await client(JoinChatlistInviteRequest(slug, input_peers))
    except (FloodWaitError, SlowModeWaitError) as e:
        decision = safety.handle_error(e)
        secs = decision.get("wait_seconds", 0)
        database.set_join_status(db_path, request_id, JR_FLOOD_DEFERRED,
                                 result="deferred", error=f"server wait {secs}s")
        return f"⏳ انتظار {secs} ثانية — استنى وأكد تاني."
    except InviteRequestSentError:
        database.set_join_status(db_path, request_id, JR_REQUESTED,
                                 result="join request sent")
        return "📩 طلبات اتبعتت — مستنية الأدمن."
    except (UsersTooMuchError, ChannelPrivateError) as e:
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="denied", error=type(e).__name__)
        return "مرفوض من تليجرام — اتقفل."
    except (InviteHashExpiredError, InviteHashInvalidError):
        database.set_join_status(db_path, request_id, JR_INVALID,
                                 result="invalid", error="folder invite invalid")
        return "الفولدر غير صالح."
    except (OSError, TimeoutError) as e:
        safety.handle_error(e)
        database.set_join_status(db_path, request_id, JR_UNCERTAIN,
                                 result="uncertain",
                                 error=f"network failure mid-join ({type(e).__name__})")
        return "⚠️ عطل شبكة — النتيجة مش مؤكدة. اتأكد بنفسك."
    except Exception as e:
        safety.handle_error(e)
        database.set_join_status(db_path, request_id, JR_DENIED,
                                 result="failed",
                                 error=f"{type(e).__name__}: {e}"[:200])
        return f"فشل ({type(e).__name__})."
    database.set_join_status(db_path, request_id, JR_JOINED, result=f"joined {n} chat(s)")
    database.log_audit(db_path, "join_success", f"request {request_id} joined folder ({n})")
    safety.record_success(f"joined folder request {request_id} ({n})")
    title = req.get("preview_title") or req["normalized_url"]
    return f"✅ انضم الفولدر «{title}»: {n} شات مرة واحدة."


def handle_cancel(db_path: Path, request_id: int) -> str:
    req = database.get_join_request(db_path, request_id)
    if req is None:
        return f"مفيش طلب بالرقم ده."
    if req["status"] not in RECONFIRMABLE:
        return f"الطلب #{request_id} خلص — مش محتاج إلغاء."
    database.set_join_status(db_path, request_id, JR_CANCELLED, result="cancelled by user")
    return f"اتلغى الطلب #{request_id}."


async def process_auto_queue(db_path: Path, client: Any, safety: Any,
                             cooldown_seconds: int = 60) -> str | None:
    """One automatic step: join the oldest pending request if everything allows.

    Returns a user-facing report, or None when there is nothing to do
    (disabled, empty queue, pacing/daily-cap/flood — all quiet by design
    to avoid Saved Messages spam; !tg status shows the state).
    Caller must ensure live mode before invoking (dry-run never auto-joins).
    """
    try:
        enabled = (database.get_setting(db_path, AUTO_ENABLED_KEY) or "0") == "1"
    except Exception:
        return None
    if not enabled:
        return None
    try:
        interval = int(database.get_setting(db_path, AUTO_INTERVAL_KEY)
                       or str(AUTO_DEFAULT_INTERVAL))
    except ValueError:
        interval = AUTO_DEFAULT_INTERVAL
    interval = min(max(interval, AUTO_MIN_INTERVAL), AUTO_MAX_INTERVAL)
    if getattr(safety, "emergency_stop", False) or getattr(safety, "joining_paused", False):
        return None
    try:
        if float(safety.flood_wait_remaining()) > 0 or float(safety.slow_wait_remaining()) > 0:
            return None
    except Exception:
        pass
    rows = database.list_join_requests(db_path, status=JR_PENDING, limit=50)
    rows += database.list_join_requests(db_path, status=JR_FLOOD_DEFERRED, limit=50)
    if not rows:
        return None
    oldest = min(rows, key=lambda r: r["id"])
    if _cooldown_remaining(db_path, interval * 60) > 0:
        return None
    if _today_count(db_path) >= _max_per_day(db_path):
        set_auto_enabled(db_path, False)
        database.log_audit(db_path, "autojoin", "daily cap reached - auto OFF")
        return "⛔ وصلت للحد اليومي — التلقائي وقف."
    result = await handle_confirm(db_path, client, safety, oldest["id"],
                                  cooldown_seconds=max(cooldown_seconds, 60),
                                  live=True)
    title = oldest.get("preview_title") or oldest["normalized_url"]
    return f"🤖 تلقائي (#{oldest['id']} | {title[:40]}):\n{result}"

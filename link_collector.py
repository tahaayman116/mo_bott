"""Link collection from new group/supergroup messages (Phase 1, read-only + notify).

- Monitors eligible groups/supergroups only; skips 1-1, broadcast channels, me.
- Prevents self-loop: ignores own/outgoing/Saved Messages messages.
- Shape-only classification (no API verification in Phase 1).
- Persistent dedup via database; notify-once default.
- DRY_RUN: save to DB, never call Telegram send.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import database
from link_parser import ParsedLink, is_group_candidate, parse_message_text

COMMAND_PREFIX = "!tg"


def _str(v: Any) -> str | None:
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return None


def should_ignore_for_collection(event: Any, self_id: int | None) -> bool:
    """True if this message must NOT be treated as a link source (loop guard)."""
    try:
        if getattr(event, "out", False):
            return True
        chat_id = getattr(event, "chat_id", None)
        sender_id = getattr(event, "sender_id", None)
        # Any message inside Saved Messages is a destination/command, never a source.
        if self_id is not None and chat_id is not None:
            try:
                if int(chat_id) == int(self_id):
                    return True
            except (TypeError, ValueError):
                if str(chat_id) == str(self_id) or str(chat_id) == "me":
                    return True
        if self_id is not None and sender_id is not None:
            try:
                if int(sender_id) == int(self_id):
                    return True
            except (TypeError, ValueError):
                pass
        # Explicit outgoing flag on message object
        msg = getattr(event, "message", None)
        if msg is not None and getattr(msg, "out", False):
            return True
    except Exception:
        return False
    return False


def is_eligible_chat(event: Any) -> bool:
    """Only basic groups and supergroups (megagroups). Duck-typed for tests.

    - private 1-1 -> False
    - broadcast channel (is_channel and not is_group) -> False
    - is_group True -> True
    - chat object with class name Chat -> True; Channel(megagroup True) -> True,
      Channel(broadcast True) -> False
    - unknown -> False (conservative)
    """
    try:
        if getattr(event, "is_private", False):
            return False
        is_group = getattr(event, "is_group", None)
        is_channel = getattr(event, "is_channel", None)
        if is_group is True:
            # megagroups report is_group True; broadcast reports is_group False/None
            if is_channel is True:
                # Could still be broadcast+group flags confusion; check chat obj.
                chat = getattr(event, "chat", None)
                if chat is not None:
                    if getattr(chat, "broadcast", False):
                        return False
                    return True
                # Telethon sets is_group True for megagroups, so allow.
                return True
            return True
        if is_channel is True:
            chat = getattr(event, "chat", None)
            if chat is not None:
                if getattr(chat, "megagroup", False):
                    return True
                if getattr(chat, "broadcast", False):
                    return False
            return False  # broadcast channel
        chat = getattr(event, "chat", None)
        if chat is not None:
            cname = type(chat).__name__
            if cname == "Chat":
                return True
            if cname == "Channel":
                if getattr(chat, "megagroup", False):
                    return True
                return False
            # Fallback flags
            if getattr(chat, "megagroup", False):
                return True
            if getattr(chat, "broadcast", False):
                return False
        return False
    except Exception:
        return False


def extract_text_and_entities(message: Any) -> tuple[str, list[str]]:
    """Return (text, entity_urls) from a Telethon Message (or mock)."""
    text = ""
    try:
        text = getattr(message, "message", None) or getattr(message, "text", None) or ""
        if not isinstance(text, str):
            text = str(text)
    except Exception:
        text = ""
    urls: list[str] = []
    try:
        entities = getattr(message, "entities", None) or []
        for e in entities:
            u = getattr(e, "url", None)
            if u and isinstance(u, str):
                urls.append(u)
    except Exception:
        pass
    return text, urls


def status_label(status: str) -> str:
    return {
        "private_invite_unverified": "دعوة خاصة (محتاجة مراجعة يدوية، مفيش انضمام تلقائي)",
        "public_link_needs_review": "لينك عام (محتاج مراجعة — ممكن جروب/قناة/شخص/بوت)",
        "folder_link_optional": "لينك فولدر (جواه جروبات — المعاينة بـ انضم)",
        "non_group_or_unrecognized": "محفوظ للمراجعة (مش مرشح كمجموعة)",
    }.get(status, status)


def format_link_notification(record: dict[str, Any], source_title: str | None,
                             source_ref: str | None, discovered: str) -> str:
    link = record.get("normalized_url", "")
    count = record.get("discovery_count", 1)
    status = record.get("status", "")
    lines = [
        "🔗 لينك جروب جديد",
        "",
        f"اللينك: {link}",
        f"المصدر: {source_title or 'غير معروف'}",
    ]
    if source_ref:
        lines.append(f"المرجع: {source_ref}")
    lines.append(f"الوقت: {discovered}")
    lines.append(f"الحالة: {status_label(status)}")
    if isinstance(count, int) and count > 1:
        lines.append(f"اتشاف {count} مرات")
    return "\n".join(lines)


def process_parsed_links(
    db_path: Path,
    parsed: list[ParsedLink],
    source_chat_id: str | None,
    source_title: str | None,
    source_username: str | None,
    text_snippet: str,
    discovered_at: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Persist links; returns (new_records, updated_records) for group candidates only."""
    ts = discovered_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    # Store ISO for DB but display short form; keep both consistent enough.
    new_records: list[dict[str, Any]] = []
    updated_records: list[dict[str, Any]] = []
    for p in parsed:
        if not is_group_candidate(p):
            continue
        is_new, rec = database.add_or_update_link(
            db_path, p, source_chat_id, source_title, source_username,
            discovered_at=None, snippet=text_snippet,
        )
        # Overwrite display time with caller-provided short form if needed elsewhere.
        if is_new:
            new_records.append(rec)
        else:
            updated_records.append(rec)
    return new_records, updated_records


def build_status_text(auth_ok: bool, connected: bool, collect_enabled: bool,
                      safety_status: dict[str, Any], db_stats: dict[str, int],
                      pending_joins: int = 0,
                      auto_state: dict[str, Any] | None = None) -> str:
    auto_line = ""
    if auto_state is not None:
        auto_line = (f"التلقائي: {'شغال' if auto_state.get('enabled') else 'مقفول'} "
                     f"(كل {auto_state.get('interval_min', '?')} دقيقة، "
                     f"النهاردة {auto_state.get('today_count', 0)}/{auto_state.get('max_per_day', '?')})")
    else:
        auto_line = "التلقائي: مقفول"
    collect_line = ""
    if auto_state is not None:
        collect_line = f"التجميع التلقائي: {'شغال' if auto_state.get('autocollect') else 'مقفول'}"
    return "\n".join([
        "📊 حالة البوت",
        f"التوثيق: {'تمام' if auth_ok else 'مش متسجل'}",
        f"الاتصال: {'متصل' if connected else 'مفصول'}",
        f"الجمع: {'شغال' if collect_enabled else 'مقفول'}",
        f"الانضمام: {'موقوف' if safety_status.get('joining_paused') else 'وضع المراجعة (تأكيد لكل طلب)'}",
        auto_line,
        collect_line,
        f"الإرسال: {'موقوف' if safety_status.get('sending_paused') else 'شغال'}",
        f"لينكات محفوظة: {db_stats.get('unique_links', 0)}",
        f"محتاجة مراجعة: {db_stats.get('pending_review', 0)}",
        f"طلبات معلقة: {pending_joins}",
        "الحملات: مقفولة (مرحلة لاحقة)",
        f"آخر نجاح: {safety_status.get('last_success') or '-'}",
        f"آخر خطأ: {safety_status.get('last_error') or '-'}",
        f"التوقف الطارئ: {'شغال' if safety_status.get('emergency_stop') else 'مقفول'}",
    ])

"""Link preview (Phase 2): read-only metadata lookup. NEVER joins.

- Public usernames -> client.get_entity (classify group/channel/user/bot).
- Private invite hashes -> messages.CheckChatInviteRequest
  (ChatInvite preview or ChatInviteAlready).
- All names below verified against installed Telethon 1.45.0.
- FloodWait/SlowMode are re-raised for the safety layer; everything else
  becomes a PreviewResult so the caller never crashes on a bad link.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telethon.errors import FloodWaitError, SlowModeWaitError
from telethon.errors.rpcerrorlist import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User
from telethon.tl.types.chatlists import ChatlistInvite, ChatlistInviteAlready

# dest_type values (stable, stored in DB preview_type):
DEST_BASIC_GROUP = "basic_group"
DEST_SUPERGROUP = "supergroup"
DEST_CHANNEL = "broadcast_channel"
DEST_USER = "user"
DEST_BOT = "bot"
DEST_PRIVATE_INVITE = "private_invite"
DEST_FOLDER = "folder_invite"
DEST_ALREADY_MEMBER = "already_member"
DEST_EXPIRED = "expired"
DEST_INVALID = "invalid"
DEST_UNKNOWN = "unknown"


@dataclass
class PreviewResult:
    ok: bool
    dest_type: str
    title: str | None
    detail: str
    ref: str  # username or invite-hash (needed for the join step)


def _display_name(entity: Any) -> str:
    try:
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        full = f"{first} {last}".strip()
        if full:
            return full
    except Exception:
        pass
    try:
        return getattr(entity, "title", None) or "؟"
    except Exception:
        return "؟"


def _folder_title_text(title_obj: Any) -> str:
    try:
        t = getattr(title_obj, "text", None)
        if t:
            return str(t)
    except Exception:
        pass
    try:
        return str(title_obj)
    except Exception:
        return "؟"


def _chat_titles(chats: Any, limit: int = 5) -> list[str]:
    out: list[str] = []
    try:
        for c in (chats or [])[:limit]:
            out.append(str(getattr(c, "title", None) or "؟"))
    except Exception:
        pass
    return out


async def preview_public(client: Any, username: str) -> PreviewResult:
    """Read-only preview of t.me/<username>. Never joins."""
    try:
        entity = await client.get_entity(username)
    except (FloodWaitError, SlowModeWaitError):
        raise
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return PreviewResult(False, DEST_INVALID, None,
                             f"اليوزر @{username} مش موجود أو محجوز لحد.", username)
    except ValueError:
        return PreviewResult(False, DEST_INVALID, None,
                             f"مش لاقي @{username} — اتأكد من الاسم.", username)
    except Exception as e:
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", username)
    try:
        if isinstance(entity, User):
            name = _display_name(entity)
            if getattr(entity, "bot", False):
                return PreviewResult(True, DEST_BOT, name,
                                     f"ده بوت ({name} @{username}) — مش جروب ومش بينضم له.", username)
            return PreviewResult(True, DEST_USER, name,
                                 f"ده حساب شخص ({name} @{username}) — مش جروب ومش بينضم له.", username)
        if isinstance(entity, Chat):
            title = getattr(entity, "title", None) or username
            return PreviewResult(True, DEST_BASIC_GROUP, title,
                                 f"جروب عادي: {title} (@{username})", username)
        if isinstance(entity, Channel):
            title = getattr(entity, "title", None) or username
            if getattr(entity, "megagroup", False):
                return PreviewResult(True, DEST_SUPERGROUP, title,
                                     f"سوبرجروب: {title} (@{username})", username)
            return PreviewResult(True, DEST_CHANNEL, title,
                                 f"قناة بث (مش جروب): {title} (@{username}) — الانضمام مسموح لو حابب.", username)
    except Exception as e:  # pragma: no cover - defensive
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", username)
    return PreviewResult(False, DEST_UNKNOWN, None, "نوع غير معروف.", username)


async def preview_invite(client: Any, invite_hash: str) -> PreviewResult:
    """Read-only preview of a private invite hash. Never joins."""
    try:
        res = await client(CheckChatInviteRequest(invite_hash))
    except (FloodWaitError, SlowModeWaitError):
        raise
    except InviteHashExpiredError:
        return PreviewResult(False, DEST_EXPIRED, None,
                             "الدعوة دي انتهت (expired) ومش هتشتغل تاني.", invite_hash)
    except InviteHashInvalidError:
        return PreviewResult(False, DEST_INVALID, None,
                             "الدعوة دي غير صالحة (invalid) — اتأكد من اللينك.", invite_hash)
    except Exception as e:
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", invite_hash)
    try:
        if isinstance(res, ChatInviteAlready):
            chat = getattr(res, "chat", None)
            title = getattr(chat, "title", None) if chat is not None else None
            return PreviewResult(True, DEST_ALREADY_MEMBER, title or "؟",
                                 f"انت عضو أصلًا في: {title or 'الجروب'} — مفيش حاجة تتعمل.", invite_hash)
        if isinstance(res, ChatInvite):
            title = getattr(res, "title", None) or "دعوة خاصة (من غير اسم ظاهر)"
            parts = getattr(res, "participants_count", 0) or 0
            extra = f" — حوالي {parts} عضو" if parts else ""
            return PreviewResult(True, DEST_PRIVATE_INVITE, title,
                                 f"دعوة خاصة: {title}{extra} (محتاجة مراجعة يدوية قبل الانضمام).",
                                 invite_hash)
    except Exception as e:  # pragma: no cover - defensive
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", invite_hash)
    return PreviewResult(False, DEST_UNKNOWN, None, "رد غير معروف من تليجرام.", invite_hash)


async def preview_folder(client: Any, slug: str) -> PreviewResult:
    """Read-only preview of a t.me/addlist/SLUG folder. Never joins.

    A folder holds several groups/channels inside; joining it joins
    all of them at once (each counts toward the daily cap).
    """
    try:
        res = await client(CheckChatlistInviteRequest(slug))
    except (FloodWaitError, SlowModeWaitError):
        raise
    except (InviteHashExpiredError, InviteHashInvalidError):
        return PreviewResult(False, DEST_INVALID, None,
                             "لينك الفولدر ده غير صالح أو انتهى — اتأكد من اللينك.", slug)
    except Exception as e:
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", slug)
    try:
        if isinstance(res, ChatlistInviteAlready):
            missing = list(getattr(res, "missing_peers", None) or [])
            if not missing:
                return PreviewResult(True, DEST_ALREADY_MEMBER, "الفولدر",
                                     "كل جروبات الفولدر ده عندك أصلًا — مفيش حاجة تتعمل.", slug)
            names = _chat_titles(getattr(res, "chats", None))
            extra = f" (منها: {', '.join(names)})" if names else ""
            return PreviewResult(True, DEST_FOLDER, "الفولدر",
                                 f"فولدر فيه {len(missing)} جروب ناقص عندك{extra} — الانضمام هيضيفهم كلهم مرة واحدة.",
                                 slug)
        if isinstance(res, ChatlistInvite):
            peers = list(getattr(res, "peers", None) or [])
            title = _folder_title_text(getattr(res, "title", None))
            names = _chat_titles(getattr(res, "chats", None))
            extra = f" (منها: {', '.join(names)})" if names else ""
            return PreviewResult(True, DEST_FOLDER, title,
                                 f"فولدر «{title}» فيه {len(peers)} شات{extra} — الانضمام هيضيفهم كلهم مرة واحدة.",
                                 slug)
    except Exception as e:  # pragma: no cover - defensive
        return PreviewResult(False, DEST_UNKNOWN, None,
                             f"المعاينة فشلت ({type(e).__name__}).", slug)
    return PreviewResult(False, DEST_UNKNOWN, None, "رد غير معروف من تليجرام.", slug)

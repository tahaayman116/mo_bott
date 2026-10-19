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
JOIN_LIMIT_MAX = 500
PREVIEW_LIST_MAX = 25
MANUAL_BTNS_MAX = 40


def clamp_join_limit(n: int) -> int:
    return max(1, min(JOIN_LIMIT_MAX, n))


def merge_links(acc: list[str], new: list[str]) -> list[str]:
    """Append new urls, de-duplicated (case-insensitive), order kept."""
    seen = {u.lower() for u in acc}
    out = list(acc)
    for u in new:
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


def prepare_join_items(urls: list[str]) -> tuple[list[tuple[str, str, str]], int]:
    """Classify + filter raw urls. Returns (items, folder_count).

    items: list of (url, kind, payload). Folders are now joinable (kind='folder'),
    so they are included in items (not skipped). folder_count = number of folders.
    """
    items: list[tuple[str, str, str]] = []
    folders = 0
    for url in urls:
        kind, payload = classify_link(url)
        if kind == "folder":
            folders += 1
            items.append((url, kind, payload))
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


def _url_for(kind: str, payload: str) -> str:
    if kind == "public":
        return f"https://t.me/{payload}"
    if kind == "invite":
        return f"https://t.me/+{payload}"
    if kind == "folder":
        return f"https://t.me/addlist/{payload}"
    return payload


def _title_text(title) -> str:
    if title is None:
        return ""
    t = getattr(title, "text", None)
    if t:
        return str(t)
    return str(title)


async def _delete_oldest_chatlist_folder(client) -> str | None:
    """Delete the oldest chatlist (shared) folder to free a slot.

    Deleting the folder filter does NOT delete its chats. Returns the deleted
    folder's title (or None if nothing was deleted).
    """
    try:
        from telethon.tl import functions, types
        res = await client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(res, "filters", None) or []
        chatlists = [f for f in filters if isinstance(f, types.DialogFilterChatlist)]
        if len(chatlists) < 2:
            return None
        oldest = sorted(chatlists, key=lambda f: getattr(f, "id", 0))[0]
        await client(functions.messages.UpdateDialogFilterRequest(
            getattr(oldest, "id", 0), None))
        return _title_text(getattr(oldest, "title", None)) or "مجلد قديم"
    except Exception:
        return None


def _to_input_peers(missing, chats, users) -> list:
    """Convert TypePeer objects from a chatlist invite into InputPeer objects.

    Bypasses Telethon's entity cache (which may be empty for freshly
    fetched invites) by resolving access_hashes from the invite's chats/users.
    """
    from telethon.tl import types
    out = []
    chat_by_id = {getattr(c, "id", None): c for c in (chats or [])}
    user_by_id = {getattr(u, "id", None): u for u in (users or [])}
    for peer in (missing or []):
        if isinstance(peer, types.PeerUser):
            u = user_by_id.get(peer.user_id)
            ah = getattr(u, "access_hash", None) if u is not None else None
            if ah is not None:
                out.append(types.InputPeerUser(peer.user_id, ah))
        elif isinstance(peer, types.PeerChat):
            out.append(types.InputPeerChat(peer.chat_id))
        elif isinstance(peer, types.PeerChannel):
            c = chat_by_id.get(peer.channel_id)
            ah = getattr(c, "access_hash", None) if c is not None else None
            if ah is not None:
                out.append(types.InputPeerChannel(peer.channel_id, ah))
            else:
                out.append(peer)  # fall back; resolve() will try cache
        else:
            out.append(peer)
    return out or list(missing or [])


def _url_for(kind: str, payload: str) -> str:
    if kind == "public":
        return f"https://t.me/{payload}"
    if kind == "invite":
        return f"https://t.me/+{payload}"
    if kind == "folder":
        return f"https://t.me/addlist/{payload}"
    return payload


async def attempt_join(client, kind: str, payload: str) -> tuple[str, list[str]]:
    """One join attempt. Returns (short Arabic result, list of links needing
    manual join because they require verification/questions)."""
    url = _url_for(kind, payload)
    try:
        if kind == "public":
            try:
                entity = await client.get_entity(payload)
            except Exception:
                return "مش لاقي اليوزر.", []
            from telethon.tl.types import Channel, Chat, User
            if isinstance(entity, User):
                return "ده حساب شخص/بوت.", []
            if isinstance(entity, Chat):
                return "جروب صغير — لازم دعوة.", []
            # Verification/question groups: skip + send link for manual join.
            if getattr(entity, "join_request", False):
                return "verify", [url]
            try:
                await client(JoinChannelRequest(entity))
                return "done", []
            except (UserAlreadyParticipantError, Exception) as e:
                if isinstance(e, UserAlreadyParticipantError) or \
                        "ALREADY_PARTICIPANT" in str(e) or "USER_ALREADY_PARTICIPANT" in str(e):
                    return "عضو أصلًا.", []
                if isinstance(e, InviteRequestSentError) or "INVITE_REQUEST_SENT" in str(e):
                    return "طلب انضمام مستني موافقة.", [url]
                if "TOO_MUCH" in str(e).upper() or "TOO_MANY" in str(e).upper():
                    return "وصلت للحد (500 جروب) — احذف بعض الجروبات أولاً.", [url]
                raise
        if kind == "invite":
            try:
                inv = await client(CheckChatInviteRequest(payload))
            except Exception:
                return "الدعوة انتهت.", []
            if getattr(inv, "request_needed", False):
                return "verify", [url]
            already = getattr(inv, "already_participant", False)
            if already:
                return "عضو أصلًا.", []
            try:
                await client(ImportChatInviteRequest(payload))
                return "done", []
            except Exception as e:
                if "FLOOD" in type(e).__name__.upper():
                    raise
                if "INVITE_REQUEST_SENT" in str(e):
                    return "طلب انضمام مستني موافقة.", [url]
                if "TOO_MUCH" in str(e).upper() or "TOO_MANY" in str(e).upper():
                    return "وصلت للحد (500 جروب) — احذف بعض الجروبات أولاً.", [url]
                return "فشل.", []
        if kind == "folder":
            from telethon.tl import functions, types
            try:
                inv = await client(functions.chatlists.CheckChatlistInviteRequest(payload))
            except Exception:
                return "الدعوة انتهت.", []
            if isinstance(inv, types.chatlists.ChatlistInviteAlready):
                missing = list(getattr(inv, "missing_peers", None) or [])
                if not missing:
                    return "عضو أصلًا (فولدر).", []
            else:
                missing = list(getattr(inv, "peers", None) or [])
                if not missing:
                    return "فولدر فارغ.", []
            total_missing = len(missing)
            input_peers = _to_input_peers(missing, getattr(inv, "chats", None),
                                          getattr(inv, "users", None))
            if not input_peers:
                input_peers = missing
            deleted = await _delete_oldest_chatlist_folder(client)
            joined = 0
            last_err = ""
            for i in range(0, len(input_peers), 20):
                batch = input_peers[i:i + 20]
                try:
                    await client(functions.chatlists.JoinChatlistInviteRequest(payload, batch))
                    joined += len(batch)
                except FloodWaitError:
                    raise
                except Exception as e:
                    last_err = type(e).__name__
                    if "TOO_MUCH" in str(e).upper() or "TOO_MANY" in str(e).upper():
                        last_err = "وصلت للحد (500 جروب)"
                        # Stop further batches.
                        break
                try:
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
            manual: list[str] = []
            if joined > 0:
                # Re-check to find chats that could not be joined (verification/etc).
                try:
                    inv2 = await client(functions.chatlists.CheckChatlistInviteRequest(payload))
                    if isinstance(inv2, types.chatlists.ChatlistInviteAlready):
                        remaining = list(getattr(inv2, "missing_peers", None) or [])
                    else:
                        remaining = list(getattr(inv2, "peers", None) or [])
                    if remaining:
                        chat_by_id = {getattr(c, "id", None): c for c in (getattr(inv, "chats", None) or [])}
                        for peer in remaining:
                            cid = getattr(peer, "channel_id", None) or getattr(peer, "chat_id", None) or getattr(peer, "user_id", None)
                            c = chat_by_id.get(cid)
                            title = _title_text(getattr(c, "title", None)) if c is not None else ""
                            label = title if title else str(cid)
                            manual.append(f"{url} — {label}")
                except Exception:
                    pass
            if joined == 0:
                if "500" in last_err:
                    return "وصلت للحد (500 جروب) — احذف بعض الجروبات أولاً.", [url]
                msg = f"فشل ({last_err})." if last_err else "فشل."
                return msg, []
            msg = f"انضميت لـ {joined} جروب في الفولدر (من {total_missing})."
            if deleted:
                msg = f"حذفت مجلد قديم ({deleted}) بدون جروباته، ثم {msg}"
            if manual:
                msg += f" ({len(manual)} يحتاج تحقق)"
            return msg, manual
    except FloodWaitError as e:
        raise
    except Exception:
        return "فشل.", []
    return "فشل.", []


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
        self._manual_links: list[str] = []

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, phone: str, items: list[tuple[str, str]],
              interval_min: int) -> None:
        self.stop()
        self.items = list(items)
        self.interval_min = max(1, min(1440, interval_min))
        self.phone = phone
        self._manual_links = []
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
        # Warn if account is near the 500-group Telegram limit.
        try:
            dialogs = await client.get_dialogs(limit=520)
            n_groups = len(dialogs)
            if n_groups >= 490:
                await self._notify(f"⚠️ الحساب يحتوي {n_groups} جروب/قناة (الحد ~500) — قد يفشل الانضمام.")
        except Exception:
            pass
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
                res, manual = await attempt_join(client, kind, payload)
            except FloodWaitError as e:
                await self._notify(f"تليجرام طلب انتظار {e.seconds} ث — وقفت.")
                break
            except Exception:
                res, manual = "فشل.", []
            database.counter_inc(self.db_path, "joins")
            if "انضميت" in res or "عضو أصلًا" in res or res == "done":
                ok += 1
                results.append(f"{i}. ✅ {res}")
            else:
                fail += 1
                label = payload if kind == "public" else (payload[:12] + "…")
                results.append(f"{i}. ❌ {label}: {res}")
            if manual:
                self._manual_links.extend(manual)
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
        if self._manual_links:
            try:
                await self._notify("📋 لينكات تحتاج انضمام يدوي:\n" + "\n".join(self._manual_links[:30]))
            except Exception:
                pass

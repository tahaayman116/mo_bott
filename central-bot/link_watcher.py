"""Monitor chats for new Telegram group links and notify the owner."""
from __future__ import annotations

import re

from telethon import events
from telethon.tl.types import ChatInviteAlready, Channel, Chat, User

from helpers import extract_links

_BARE_TME_RE = re.compile(r"(?<![A-Za-z0-9])t\.me/[^\s<>()\u0600-\u06FF]+",
                          re.IGNORECASE)


def extract_all_links(text: str) -> list[str]:
    """http(s) URLs plus bare t.me links, de-duplicated, order kept."""
    found = extract_links(text)
    seen = {u.lower() for u in found}
    for m in _BARE_TME_RE.finditer(text or ""):
        url = "https://" + m.group(0).rstrip(".,;:!?)\\]}'\"*_~")
        key = url.lower()
        if key not in seen:
            seen.add(key)
            found.append(url)
    return found


class LinkWatcher:
    def __init__(self, get_client, notify, db_path):
        self._get_client = get_client
        self._notify = notify
        self._db_path = db_path
        self._handler = None
        self._client = None
        self._seen: set[str] = set()
        self._classify_cache: dict[str, str] = {}
        self._enabled = False

    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def client(self):
        return self._client

    async def set_enabled(self, enabled: bool) -> bool:
        self._enabled = enabled
        if enabled:
            return await self.start()
        await self.stop()
        return True

    async def start(self) -> bool:
        if not self._enabled:
            return False
        if self._handler is not None:
            return True
        try:
            phone, client = await self._get_client()
            self._client = client
            self._handler = client.on(events.NewMessage(incoming=True))(self._on_message)
            return True
        except Exception:
            self._handler = None
            self._client = None
            return False

    async def stop(self):
        if self._handler is not None and self._client is not None:
            try:
                self._client.remove_event_handler(self._handler)
            except Exception:
                pass
        self._handler = None
        self._client = None

    async def restart(self):
        await self.stop()
        await self.start()

    async def _classify(self, client, url: str) -> str:
        """Return 'group', 'channel', or 'unknown'."""
        if url in self._classify_cache:
            return self._classify_cache[url]
        result = "unknown"
        try:
            m = re.search(r"t\.me/([A-Za-z][A-Za-z0-9_]{3,31})$", url)
            if m:
                username = m.group(1)
                ent = await client.get_entity(username)
                if isinstance(ent, User):
                    result = "unknown"
                elif isinstance(ent, Chat):
                    result = "group"
                elif isinstance(ent, Channel):
                    result = "group" if not getattr(ent, "broadcast", False) else "channel"
            elif "t.me/+" in url or "t.me/joinchat/" in url:
                slug = url.split("/")[-1].lstrip("+").replace("joinchat/", "")
                try:
                    from telethon.tl.functions.messages import CheckChatInviteRequest
                    inv = await client(CheckChatInviteRequest(slug))
                    if isinstance(inv, ChatInviteAlready):
                        ent = inv.chat
                        if isinstance(ent, Chat):
                            result = "group"
                        elif isinstance(ent, Channel):
                            result = "group" if not getattr(ent, "broadcast", False) else "channel"
                    elif getattr(inv, "broadcast", False):
                        result = "channel"
                    else:
                        result = "group"
                except Exception:
                    try:
                        from telethon.tl.functions.chatlists import CheckChatlistInviteRequest
                        await client(CheckChatlistInviteRequest(slug))
                        result = "group"
                    except Exception:
                        result = "unknown"
            elif "t.me/addlist/" in url:
                result = "group"
        except Exception:
            result = "unknown"
        self._classify_cache[url] = result
        return result

    async def _on_message(self, event):
        if not self._enabled:
            return
        try:
            if not event.is_group:
                return
        except Exception:
            return
        text = event.message.message or ""
        links = extract_all_links(text)
        if not links:
            return
        client = self._client
        if client is None:
            return
        new_links: list[str] = []
        for link in links:
            if link in self._seen:
                continue
            try:
                kind = await self._classify(client, link)
            except Exception:
                kind = "unknown"
            if kind != "group":
                continue
            self._seen.add(link)
            new_links.append(link)
        if new_links:
            try:
                await self._notify("🔗 لينكات جروبات جديدة:\n"
                                   + "\n".join(new_links))
            except Exception:
                pass

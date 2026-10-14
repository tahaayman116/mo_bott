"""Collect groups+channels a phone number is already in.

Public (has username) -> t.me link. Private -> title + admin invite if the
account is admin, else title only. Chunked for Telegram's message cap.
"""
from __future__ import annotations

from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import Channel, Chat

import database
from helpers import chunk_text


async def collect_dialogs(client) -> list[dict]:
    items: list[dict] = []
    async for dlg in client.iter_dialogs():
        ent = dlg.entity
        if isinstance(ent, Channel):
            kind = "قناة" if getattr(ent, "broadcast", False) else "جروب"
        elif isinstance(ent, Chat):
            kind = "جروب"
        else:
            continue
        title = getattr(ent, "title", "") or "بدون اسم"
        username = getattr(ent, "username", None)
        if username:
            items.append({"kind": kind, "title": title,
                          "link": f"https://t.me/{username}"})
            continue
        link = ""
        try:
            if getattr(ent, "creator", False) or getattr(ent, "admin_rights", None):
                res = await client(ExportChatInviteRequest(ent))
                link = getattr(res, "link", "") or ""
        except Exception:
            link = ""
        items.append({"kind": kind, "title": title, "link": link})
    return items


def format_collect(phone: str, items: list[dict]) -> list[str]:
    groups = [i for i in items if i["kind"] == "جروب"]
    channels = [i for i in items if i["kind"] == "قناة"]
    lines = [f"📋 الرقم {phone}: {len(groups)} جروب + {len(channels)} قناة", ""]
    n = 0
    for i in items:
        n += 1
        if i["link"]:
            lines.append(f"{n}. [{i['kind']}] {i['title']}\n{i['link']}")
        else:
            lines.append(f"{n}. [{i['kind']}] {i['title']} (خاص — بدون رابط)")
    return chunk_text("\n".join(lines))


def save_and_format(db_path, phone: str, items: list[dict]) -> list[str]:
    database.save_collected(db_path, phone, items)
    return format_collect(phone, items)


def format_links_block(items: list[dict]) -> list[str]:
    """All public links as copy-friendly code blocks (tap -> copy on mobile).

    Returns [] when there is nothing public to copy.
    """
    links = [i["link"] for i in items if i.get("link")]
    if not links:
        return []
    blocks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for link in links:
        if cur_len + len(link) + 1 > 3900 and cur:
            blocks.append("```\n" + "\n".join(cur) + "\n```")
            cur, cur_len = [], 0
        cur.append(link)
        cur_len += len(link) + 1
    if cur:
        blocks.append("```\n" + "\n".join(cur) + "\n```")
    return blocks

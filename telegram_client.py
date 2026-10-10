"""Telegram client + safe first-time auth (no secrets in logs)."""
from __future__ import annotations

import getpass
import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from config import AppConfig

log = logging.getLogger("userbot.auth")


def build_client(cfg: AppConfig) -> TelegramClient:
    data_dir = cfg.project_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    session_path = str(data_dir / cfg.session_name)
    # Keep default flood_sleep_threshold (auto-sleep small waits); safety layer
    # handles longer waits explicitly. Never set to 0 blindly.
    return TelegramClient(session_path, cfg.api_id, cfg.api_hash)


async def ensure_authorized(client: TelegramClient, cfg: AppConfig) -> object:
    """Connect + login with phone/code/2FA prompts. Returns self user. No secret logging."""
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        log.info("Already authorized.")
        return me
    phone = cfg.phone or input("Enter phone (+countrycode...): ").strip()
    await client.send_code_request(phone)
    code = input("Enter Telegram login code: ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pw = getpass.getpass("Enter 2FA password (hidden, not stored): ")
        await client.sign_in(password=pw)
        del pw
    me = await client.get_me()
    log.info("Authorization successful.")
    return me


async def validate_account(client: TelegramClient) -> object:
    me = await client.get_me()
    if me is None:
        raise RuntimeError("Not authorized (get_me returned None). Delete data/*.session and retry.")
    return me

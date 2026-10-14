"""One-time console login for a phone number (Windows/Termux terminal).

WHY: Telegram burns any login code that appears inside a Telegram chat
("previously shared" block). So the code must be typed HERE in the console,
never pasted into any chat. Created session lands in sessions/ and the
central bot picks it up (switch via the numbers button).

Usage:
    python login_once.py +201012345678
"""
from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

from config import load_config
from database import init_db
from helpers import normalize_phone
from session_manager import LoginError, SessionManager


async def _run(phone: str, sm: SessionManager) -> int:
    try:
        await sm.start_login(phone)
    except LoginError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"Code sent to {phone}. Type it HERE (never paste it in any chat):")
    try:
        code = input("code: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        await sm.cancel_login(phone)
        return 1
    try:
        res = await sm.submit_code(phone, code)
    except LoginError as e:
        print(f"FAILED: {e}\nWait a bit, then run again (use the LATEST code).")
        return 1
    if res == "need_password":
        try:
            pwd = getpass.getpass("2FA password (hidden): ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            await sm.cancel_login(phone)
            return 1
        try:
            await sm.submit_password(phone, pwd)
        except LoginError as e:
            print(f"FAILED: {e}")
            return 1
    print(f"OK: {phone} registered. Open the control bot -> numbers -> tap it.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python login_once.py +201012345678")
        return 2
    phone = normalize_phone(argv[1])
    if not phone:
        print("Bad phone. Example: python login_once.py +201012345678")
        return 2
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Config error:\n{e}")
        return 2
    init_db(cfg.db_path)
    sm = SessionManager(cfg.api_id, cfg.api_hash, cfg.sessions_dir, cfg.db_path)
    return asyncio.run(_run(phone, sm))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

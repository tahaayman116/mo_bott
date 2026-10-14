"""Configuration loading and validation (central bot).

Reads local .env only. Never prints secrets.
One API_ID/API_HASH pair drives ALL phone numbers (app keys, not account keys).
The control bot answers ONLY to OWNER_ID.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):  # type: ignore
        return False


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    bot_token: str
    owner_id: int
    db_path: Path
    sessions_dir: Path
    post_daily_cap: int
    log_level: str
    project_root: Path


def project_root_from_here() -> Path:
    return Path(__file__).resolve().parent


def load_config(env_path: Path | None = None, root: Path | None = None) -> AppConfig:
    base = root or project_root_from_here()
    dotenv_path = env_path or (base / ".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path=dotenv_path)

    def _get(name: str) -> str | None:
        v = os.getenv(name)
        if v is not None and v.strip() == "":
            return None
        return v

    api_id_raw = _get("API_ID")
    api_hash = _get("API_HASH")
    token = _get("CONTROL_BOT_TOKEN")
    owner_raw = _get("OWNER_ID")

    problems: list[str] = []
    api_id = 0
    if not api_id_raw:
        problems.append("API_ID is missing. Copy .env.example to .env and set it (https://my.telegram.org/apps).")
    else:
        try:
            api_id = int(api_id_raw.strip())
        except ValueError:
            problems.append("API_ID must be digits only, no spaces.")
    if not api_hash or len(api_hash.strip()) < 10:
        problems.append("API_HASH is missing or too short. Set it in .env.")
    if not token or len(token.strip()) < 20:
        problems.append("CONTROL_BOT_TOKEN is missing. Create a bot via @BotFather and paste its token in .env.")
    owner_id = 0
    if not owner_raw:
        problems.append("OWNER_ID is missing. Talk to @userinfobot, copy your numeric id into .env.")
    else:
        try:
            owner_id = int(owner_raw.strip())
        except ValueError:
            problems.append("OWNER_ID must be your numeric Telegram id.")
    if problems:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(problems))

    def _int(name: str, default: int) -> int:
        try:
            return int((_get(name) or "").strip() or default)
        except ValueError:
            return default

    db_path = Path(_get("DB_PATH") or "data/central.db")
    sessions_dir = Path(_get("SESSIONS_DIR") or "sessions")
    if not db_path.is_absolute():
        db_path = base / db_path
    if not sessions_dir.is_absolute():
        sessions_dir = base / sessions_dir

    return AppConfig(
        api_id=api_id,
        api_hash=(api_hash or "").strip(),
        bot_token=(token or "").strip(),
        owner_id=owner_id,
        db_path=db_path,
        sessions_dir=sessions_dir,
        post_daily_cap=_int("POST_DAILY_CAP", 100),
        log_level=(_get("LOG_LEVEL") or "INFO").strip().upper() or "INFO",
        project_root=base,
    )

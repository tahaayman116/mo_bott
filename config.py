"""Configuration loading and validation (Phase 1).

Reads local .env only. Never prints secrets. Conservative defaults:
DRY_RUN=true, ALLOW_LIVE_SEND=false, COLLECT_ENABLED=true.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is in requirements but keep import safe
    def load_dotenv(*args, **kwargs):  # type: ignore
        return False


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    phone: str | None
    session_name: str
    db_path: Path
    dry_run: bool
    allow_live_send: bool
    collect_enabled: bool
    duplicate_notify: bool
    log_level: str
    project_root: Path
    join_cooldown_seconds: int = 60


def project_root_from_here() -> Path:
    return Path(__file__).resolve().parent


def load_config(env_path: Path | None = None, root: Path | None = None) -> AppConfig:
    """Load and validate config. Raises ValueError with actionable message on failure."""
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
    phone = _get("PHONE")
    session_name = (_get("SESSION_NAME") or "userbot_session").strip()
    db_raw = (_get("DB_PATH") or "data/userbot.db").strip()
    dry_run = _parse_bool(os.getenv("DRY_RUN"), True)
    allow_live_send = _parse_bool(os.getenv("ALLOW_LIVE_SEND"), False)
    collect_enabled = _parse_bool(os.getenv("COLLECT_ENABLED"), True)
    duplicate_notify = _parse_bool(os.getenv("DUPLICATE_NOTIFY"), False)
    log_level = ((_get("LOG_LEVEL") or "INFO").strip().upper())
    try:
        join_cooldown_seconds = int((_get("JOIN_COOLDOWN_SECONDS") or "60").strip())
        if join_cooldown_seconds < 0:
            errors.append("JOIN_COOLDOWN_SECONDS must be 0 or more.")
    except ValueError:
        errors.append("JOIN_COOLDOWN_SECONDS must be a number of seconds.")
        join_cooldown_seconds = 60

    errors: list[str] = []
    api_id: int = 0
    if not api_id_raw:
        errors.append("API_ID is missing. Copy .env.example to .env and set API_ID from https://my.telegram.org/apps")
    else:
        try:
            api_id = int(api_id_raw.strip())
            if api_id <= 0:
                errors.append("API_ID must be a positive integer.")
        except ValueError:
            errors.append("API_ID must be a number (from https://my.telegram.org/apps).")
    if not api_hash or len(api_hash.strip()) < 8:
        errors.append("API_HASH is missing or too short. Set it in .env from https://my.telegram.org/apps")
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        errors.append(f"LOG_LEVEL must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL (got {log_level}).")
    if errors:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(errors))

    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = base / db_path

    assert api_hash is not None
    return AppConfig(
        api_id=api_id,
        api_hash=api_hash.strip(),
        phone=phone.strip() if phone else None,
        session_name=session_name,
        db_path=db_path,
        dry_run=dry_run,
        allow_live_send=allow_live_send,
        collect_enabled=collect_enabled,
        duplicate_notify=duplicate_notify,
        log_level=log_level,
        project_root=base,
        join_cooldown_seconds=join_cooldown_seconds,
    )


def is_live_notifications_enabled(cfg: AppConfig) -> bool:
    """Real Telegram sends are allowed only when user explicitly opted in.

    Requires BOTH DRY_RUN=false AND ALLOW_LIVE_SEND=true AND COLLECT_ENABLED=true.
    Default (.env.example) keeps everything in dry-run.
    """
    return (not cfg.dry_run) and cfg.allow_live_send and cfg.collect_enabled


def session_file_path(cfg: AppConfig) -> Path:
    d = cfg.project_root / "data"
    return d / f"{cfg.session_name}.session"

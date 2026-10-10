"""Safety + error classification using REAL Telethon exceptions (no invented names).

Policy:
- FloodWait / SlowMode: honor server seconds exactly, pause outgoing, notify.
- Expired/invalid/already-participant: permanent, never retry.
- Session/auth failures: critical, stop safely.
- Transient network only: bounded exponential backoff (max 5, cap 60s).
- Read-only collection stays allowed; joining/sending paused independently.
- No interval is a guarantee against restrictions.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

# Import only names that really exist in the installed Telethon.
try:  # Telethon >=1.x exposes these at telethon.errors
    from telethon.errors import FloodWaitError, SlowModeWaitError  # type: ignore
except Exception:  # pragma: no cover
    FloodWaitError = None  # type: ignore
    SlowModeWaitError = None  # type: ignore

try:
    from telethon.errors.rpcerrorlist import (  # type: ignore
        InviteHashExpiredError,
        InviteHashInvalidError,
        UserAlreadyParticipantError,
    )
except Exception:  # pragma: no cover
    InviteHashExpiredError = None  # type: ignore
    InviteHashInvalidError = None  # type: ignore
    UserAlreadyParticipantError = None  # type: ignore

try:
    from telethon.errors.rpcerrorlist import (  # type: ignore
        AuthKeyUnregisteredError,
        SessionRevokedError,
        UserDeactivatedError,
    )
except Exception:  # pragma: no cover
    AuthKeyUnregisteredError = None  # type: ignore
    SessionRevokedError = None  # type: ignore
    UserDeactivatedError = None  # type: ignore


def _cls_names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


@dataclass
class SafetyManager:
    flood_until: float = 0.0
    slow_until: float = 0.0
    emergency_stop: bool = False
    joining_paused: bool = True  # Phase 1: joining always disabled
    sending_paused: bool = False
    last_success: str | None = None
    last_error: str | None = None
    transient_failures: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    # ---- queries ----
    def flood_wait_remaining(self) -> float:
        return max(0.0, self.flood_until - time.time())

    def slow_wait_remaining(self) -> float:
        return max(0.0, self.slow_until - time.time())

    def is_collection_allowed(self) -> bool:
        # Read-only collection keeps running even when outgoing is paused.
        return True

    def is_outgoing_allowed(self) -> bool:
        if self.emergency_stop:
            return False
        if self.flood_wait_remaining() > 0:
            return False
        if self.slow_wait_remaining() > 0:
            return False
        return not self.sending_paused

    def activate_emergency_stop(self, reason: str = "") -> None:
        self.emergency_stop = True
        self.sending_paused = True
        self.joining_paused = True
        self.last_error = f"EMERGENCY STOP: {reason}"[:500]
        self.events.append({"type": "emergency_stop", "reason": reason})

    def clear_emergency_stop(self) -> None:
        self.emergency_stop = False

    def record_success(self, label: str) -> None:
        self.last_success = label[:500]
        self.transient_failures = 0

    # ---- classification ----
    def handle_error(self, exc: BaseException) -> dict[str, Any]:
        names = _cls_names(exc)
        seconds = getattr(exc, "seconds", None)

        def _available(cls):
            return cls is not None and isinstance(exc, cls)

        # 1) FloodWait
        if _available(FloodWaitError) or "FloodWaitError" in names:
            wait = int(seconds) if isinstance(seconds, int) else 60
            self.flood_until = time.time() + max(wait, 1)
            self.sending_paused = True
            self.last_error = f"FloodWait: must wait {wait}s (server-enforced)"[:500]
            return {"action": "wait", "wait_seconds": wait, "retry": False,
                    "notify": True, "category": "telegram_flood"}
        # 2) SlowMode
        if _available(SlowModeWaitError) or "SlowModeWaitError" in names:
            wait = int(seconds) if isinstance(seconds, int) else 30
            self.slow_until = time.time() + max(wait, 1)
            self.last_error = f"SlowMode: must wait {wait}s"[:500]
            return {"action": "wait", "wait_seconds": wait, "retry": False,
                    "notify": True, "category": "telegram_slowmode"}
        # 3) Permanent invite problems: never retry
        if (_available(InviteHashExpiredError) or "InviteHashExpiredError" in names
                or "InviteHashInvalidError" in names or _available(InviteHashInvalidError)):
            self.last_error = f"Permanent invite error: {type(exc).__name__}"[:500]
            return {"action": "drop", "wait_seconds": 0, "retry": False,
                    "notify": False, "category": "telegram_permanent"}
        if _available(UserAlreadyParticipantError) or "UserAlreadyParticipantError" in names:
            self.last_error = "Already a participant"[:500]
            return {"action": "drop", "wait_seconds": 0, "retry": False,
                    "notify": False, "category": "telegram_already"}
        # 4) Session/auth critical
        if (names & {"AuthKeyUnregisteredError", "SessionRevokedError",
                     "UserDeactivatedError", "UnauthorizedError", "AuthKeyError"}):
            self.last_error = f"Session/auth critical: {type(exc).__name__}"[:500]
            self.sending_paused = True
            self.joining_paused = True
            return {"action": "stop", "wait_seconds": 0, "retry": False,
                    "notify": True, "category": "auth_critical"}
        # 5) Transient network only -> bounded backoff
        if isinstance(exc, (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError)):
            self.transient_failures += 1
            if self.transient_failures > 5:
                self.last_error = f"Network failing repeatedly, pausing for review: {exc!r}"[:500]
                return {"action": "pause_for_review", "wait_seconds": 60, "retry": False,
                        "notify": True, "category": "network"}
            wait = min(2 ** self.transient_failures, 60)
            self.last_error = f"Transient network ({self.transient_failures}/5), backoff {wait}s"[:500]
            return {"action": "backoff", "wait_seconds": wait, "retry": True,
                    "notify": False, "category": "network"}
        # 6) Unknown API error: do NOT endless-retry
        self.last_error = f"API error {type(exc).__name__}: {exc}"[:500]
        return {"action": "pause_for_review", "wait_seconds": 0, "retry": False,
                "notify": True, "category": "unknown"}

    def status_dict(self) -> dict[str, Any]:
        return {
            "emergency_stop": self.emergency_stop,
            "joining_paused": self.joining_paused,
            "sending_paused": self.sending_paused,
            "flood_wait_remaining": int(self.flood_wait_remaining()),
            "slow_wait_remaining": int(self.slow_wait_remaining()),
            "outgoing_allowed": self.is_outgoing_allowed(),
            "last_success": self.last_success,
            "last_error": self.last_error,
        }

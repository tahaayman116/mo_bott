"""Safety manager tests with REAL Telethon error classes (no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from safety_manager import SafetyManager


def _make(cls_name, **kw):
    try:
        mod = __import__("telethon.errors.rpcerrorlist", fromlist=[cls_name])
        cls = getattr(mod, cls_name)
        try:
            # Most Telethon errors accept (request, message). Build minimal fake.
            return cls(request=None, message=cls_name)
        except Exception:
            try:
                obj = object.__new__(cls)
                for k, v in kw.items():
                    setattr(obj, k, v)
                return obj
            except Exception:
                return Exception(cls_name)
    except Exception:
        e = Exception(cls_name)
        for k, v in kw.items():
            setattr(e, k, v)
        type(e).__name__ = cls_name
        return e


def test_flood_honors_seconds_and_pauses_outgoing():
    sm = SafetyManager()
    from telethon.errors import FloodWaitError

    exc = FloodWaitError(request=None, capture=42)
    assert exc.seconds == 42  # real Telethon field (installed v1.45.0)
    d = sm.handle_error(exc)
    assert d["wait_seconds"] == 42
    assert d["retry"] is False
    assert sm.flood_wait_remaining() > 0
    assert sm.is_outgoing_allowed() is False
    assert sm.is_collection_allowed() is True  # read-only stays on


def test_slowmode_wait():
    sm = SafetyManager()
    from telethon.errors import SlowModeWaitError

    exc = SlowModeWaitError(request=None, capture=15)
    assert exc.seconds == 15
    d = sm.handle_error(exc)
    assert d["wait_seconds"] == 15
    assert sm.is_outgoing_allowed() is False


def test_permanent_invite_no_retry():
    sm = SafetyManager()
    from telethon.errors.rpcerrorlist import (
        InviteHashExpiredError,
        InviteHashInvalidError,
        UserAlreadyParticipantError,
    )

    for cls in (InviteHashExpiredError, InviteHashInvalidError, UserAlreadyParticipantError):
        exc = cls(request=None)
        d = sm.handle_error(exc)
        assert d["retry"] is False
        assert d["action"] == "drop"


def test_transient_backoff_bounded():
    sm = SafetyManager()
    for i in range(5):
        d = sm.handle_error(OSError("net down"))
        assert d["category"] == "network"
    d6 = sm.handle_error(OSError("still down"))
    assert d6["action"] == "pause_for_review"
    assert d6["wait_seconds"] <= 60


def test_emergency_stop_blocks_outgoing_not_collection():
    sm = SafetyManager()
    sm.activate_emergency_stop("manual test")
    assert sm.is_outgoing_allowed() is False
    assert sm.is_collection_allowed() is True
    assert sm.status_dict()["emergency_stop"] is True


def test_unknown_error_does_not_endless_retry():
    sm = SafetyManager()
    d = sm.handle_error(ValueError("weird"))
    assert d["retry"] is False

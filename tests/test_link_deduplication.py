"""Dedup + persistence + restart + loop-guard tests (mocked, no Telegram)."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from link_parser import parse_message_text
from link_collector import (
    is_eligible_chat,
    process_parsed_links,
    should_ignore_for_collection,
)


class FakeEvent:
    def __init__(self, chat_id=123, sender_id=456, out=False,
                 is_group=True, is_channel=False, is_private=False, chat=None):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.out = out
        self.is_group = is_group
        self.is_channel = is_channel
        self.is_private = is_private
        self.chat = chat


def _tmpdb():
    d = Path(tempfile.mkdtemp())
    p = d / "t.db"
    database.init_db(p)
    return p


def test_duplicate_persistence_counts():
    db = _tmpdb()
    parsed = parse_message_text("see https://t.me/+AAA111bbb222")
    n1, u1 = process_parsed_links(db, parsed, "1", "G1", None, "snippet")
    assert len(n1) == 1 and u1 == []
    n2, u2 = process_parsed_links(db, parsed, "1", "G1", None, "snippet")
    assert n2 == [] and len(u2) == 1
    rec = database.get_link(db, "https://t.me/+AAA111bbb222")
    assert rec["discovery_count"] == 2
    assert rec["first_seen"] <= rec["last_seen"]


def test_normalized_equivalence_dedups():
    db = _tmpdb()
    a = parse_message_text("x https://telegram.me/example_group")
    b = parse_message_text("x http://www.t.me/example_group/")
    process_parsed_links(db, a, "1", "G", None, "s")
    n2, _ = process_parsed_links(db, b, "2", "G2", None, "s")
    assert n2 == []  # same normalized -> duplicate
    assert database.get_stats(db)["unique_links"] == 1


def test_restart_survives(tmp_path=None):
    import tempfile
    d = Path(tempfile.mkdtemp())
    db = d / "r.db"
    database.init_db(db)
    process_parsed_links(db, parse_message_text("https://t.me/+Restart123ABC"), "9", "G9", None, "s")
    # simulate restart: re-init + read
    database.init_db(db)
    rec = database.get_link(db, "https://t.me/+Restart123ABC")
    assert rec is not None and rec["discovery_count"] == 1


def test_own_saved_messages_ignored_loop_guard():
    me = 777
    assert should_ignore_for_collection(FakeEvent(chat_id=me, sender_id=me), me)
    assert should_ignore_for_collection(FakeEvent(chat_id=me, sender_id=999), me)
    assert should_ignore_for_collection(FakeEvent(chat_id=111, sender_id=me), me)
    assert should_ignore_for_collection(FakeEvent(chat_id=111, sender_id=222, out=True), me)
    assert not should_ignore_for_collection(FakeEvent(chat_id=111, sender_id=222), me)


def test_eligibility_groups_only():
    assert is_eligible_chat(FakeEvent(is_group=True, is_channel=False, is_private=False))
    # megagroup flags
    assert is_eligible_chat(FakeEvent(is_group=True, is_channel=True, is_private=False))
    assert not is_eligible_chat(FakeEvent(is_group=False, is_channel=True, is_private=False))
    assert not is_eligible_chat(FakeEvent(is_private=True))
    # unknown -> conservative False
    class E:
        pass
    assert not is_eligible_chat(E())


def test_non_group_not_notified_but_ambiguous_kept():
    db = _tmpdb()
    stickers = parse_message_text("https://t.me/addstickers/x")
    n, _ = process_parsed_links(db, stickers, "1", "G", None, "s")
    assert n == []  # not a group candidate -> no notification record
    amb = parse_message_text("https://t.me/some_public_name")
    n2, _ = process_parsed_links(db, amb, "1", "G", None, "s")
    assert len(n2) == 1  # ambiguous public kept for review + notified once

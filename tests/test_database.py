"""DB recovery/migration/settings tests."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from link_parser import parse_message_text


def _tmpdb(name="d.db"):
    d = Path(tempfile.mkdtemp())
    p = d / name
    database.init_db(p)
    return p


def test_init_idempotent_and_version():
    db = _tmpdb()
    database.init_db(db)
    database.init_db(db)
    assert database.get_setting(db, "schema_version") == str(database.SCHEMA_VERSION)


def test_unique_constraint_and_stats():
    db = _tmpdb()
    database.add_or_update_link(db, parse_message_text("https://t.me/+AbC111222333")[0],
                                "1", "G1", None, snippet="hi")
    database.add_or_update_link(db, parse_message_text("https://t.me/+AbC111222333")[0],
                                "2", "G2", None, snippet="hi2")
    stats = database.get_stats(db)
    assert stats["unique_links"] == 1
    assert stats["total_events"] == 2


def test_settings_audit_known_chats():
    db = _tmpdb()
    database.set_setting(db, "k", "v")
    assert database.get_setting(db, "k") == "v"
    database.log_audit(db, "test", "hello")
    database.upsert_known_chat(db, "123", "MyGroup", "mygroup", "group")
    database.upsert_known_chat(db, "123", "MyGroup2", "mygroup", "group")


def test_mark_notified_for_future_resend():
    db = _tmpdb()
    p = parse_message_text("https://t.me/my_public_group")[0]
    database.add_or_update_link(db, p, "1", "G", None, snippet="s")
    database.mark_notified(db, p.normalized_url)
    rec = database.get_link(db, p.normalized_url)
    assert rec["notify_count"] == 1
    assert rec["last_notified_at"] is not None


def test_backup(tmp_path):
    import tempfile
    d = Path(tempfile.mkdtemp())
    src = d / "a.db"
    dst = d / "b.db"
    database.init_db(src)
    database.add_or_update_link(src, parse_message_text("https://t.me/+Backup123456")[0],
                                "1", "G", None, snippet="s")
    out = database.backup_db(src, dst)
    assert out.exists()
    assert database.get_link(dst, "https://t.me/+Backup123456") is not None

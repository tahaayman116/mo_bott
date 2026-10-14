import tempfile
from pathlib import Path

import database


def _tmpdb():
    d = tempfile.mkdtemp()
    p = Path(d) / "t.db"
    database.init_db(p)
    return p


def test_accounts_and_active():
    db = _tmpdb()
    assert database.list_accounts(db) == []
    assert database.get_active_phone(db) == ""
    database.add_account(db, "+201000000001", "sessions/x.session", "Taha")
    database.add_account(db, "+201000000002", "sessions/y.session", "")
    accs = database.list_accounts(db)
    assert len(accs) == 2
    assert database.get_account(db, "+201000000001")["display_name"] == "Taha"
    database.set_active_phone(db, "+201000000002")
    assert database.get_active_phone(db) == "+201000000002"
    database.remove_account(db, "+201000000002")
    assert database.get_active_phone(db) == ""  # active cleared
    assert len(database.list_accounts(db)) == 1


def test_counters_and_broadcast():
    db = _tmpdb()
    assert database.counter_get(db, "joins") == 0
    database.counter_inc(db, "joins")
    database.counter_inc(db, "joins")
    assert database.counter_get(db, "joins") == 2
    bid = database.create_broadcast(db, "+2010", "hello", 5, 10)
    assert bid == 1
    database.update_broadcast(db, bid, status="done", done_ok=9, done_fail=1)
    database.save_collected(db, "+2010", [{"kind": "جروب", "title": "G", "link": "https://t.me/g"}])

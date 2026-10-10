"""Join manager + verifier tests (mocked client, no Telegram account)."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import database
import join_manager
from link_verifier import preview_invite, preview_public
from safety_manager import SafetyManager
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)
from telethon.tl.types import Channel, Chat, ChatInvite, ChatInviteAlready, User


def _tmpdb():
    p = Path(tempfile.mkdtemp()) / "j.db"
    database.init_db(p)
    return p


def _user(username="someone", bot=False):
    u = User.__new__(User)
    u.bot = bot
    u.first_name = "Test"
    u.last_name = None
    u.username = username
    return u


def _group(title="G", username="mygroup"):
    c = Channel.__new__(Channel)
    c.megagroup = True
    c.broadcast = False
    c.title = title
    c.username = username
    return c


def _channel(title="C", username="mychannel"):
    c = Channel.__new__(Channel)
    c.megagroup = False
    c.broadcast = True
    c.title = title
    c.username = username
    return c


class FakeClient:
    """Minimal Telethon-like client. get_entity/get_input_entity + __call__."""

    def __init__(self, entities=None, invite_result=None, invite_error=None,
                 join_error=None, flood_on=None, folder_result=None,
                 folder_error=None, folder_join_error=None):
        self.entities = entities or {}
        self.invite_result = invite_result
        self.invite_error = invite_error
        self.join_error = join_error
        self.flood_on = flood_on  # 'entity' | 'invite' | 'join' | 'folder' | None
        self.folder_result = folder_result
        self.folder_error = folder_error
        self.folder_join_error = folder_join_error
        self.calls = []

    async def get_entity(self, username):
        self.calls.append(("get_entity", username))
        if self.flood_on == "entity":
            raise FloodWaitError(request=None, capture=30)
        if username not in self.entities:
            raise ValueError("not found")
        return self.entities[username]

    async def get_input_entity(self, peer):
        self.calls.append(("get_input_entity", peer))
        return peer  # stand-in input peer

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append((name, request))
        if name == "CheckChatInviteRequest":
            if self.flood_on == "invite":
                raise FloodWaitError(request=None, capture=20)
            if self.invite_error:
                raise self.invite_error
            return self.invite_result
        if name == "CheckChatlistInviteRequest":
            if self.flood_on == "folder":
                raise FloodWaitError(request=None, capture=25)
            if self.folder_error:
                raise self.folder_error
            return self.folder_result
        if name in ("JoinChannelRequest", "ImportChatInviteRequest"):
            if self.flood_on == "join":
                raise FloodWaitError(request=None, capture=45)
            if self.join_error:
                raise self.join_error
            return object()  # success Updates-like
        if name == "JoinChatlistInviteRequest":
            if self.flood_on == "folderjoin":
                raise FloodWaitError(request=None, capture=50)
            if self.folder_join_error:
                raise self.folder_join_error
            return object()
        raise AssertionError(f"unexpected request {name}")


def _folder_invite(title="Pack", n=2):
    from types import SimpleNamespace
    from telethon.tl.types import PeerChannel
    from telethon.tl.types.chatlists import ChatlistInvite
    inv = ChatlistInvite.__new__(ChatlistInvite)
    inv.title = SimpleNamespace(text=title)
    inv.peers = [PeerChannel(channel_id=100 + i) for i in range(n)]
    chats = []
    for i in range(n):
        ch = Channel.__new__(Channel)
        ch.megagroup = True
        ch.broadcast = False
        ch.title = f"F{i}"
        chats.append(ch)
    inv.chats = chats
    inv.users = []
    return inv


def _folder_already(missing=0):
    from telethon.tl.types import PeerChannel
    from telethon.tl.types.chatlists import ChatlistInviteAlready
    a = ChatlistInviteAlready.__new__(ChatlistInviteAlready)
    a.missing_peers = [PeerChannel(channel_id=200 + i) for i in range(missing)]
    a.already_peers = []
    a.chats = []
    a.users = []
    return a


def _invite(title="Priv"):
    i = ChatInvite.__new__(ChatInvite)
    i.title = title
    i.participants_count = 12
    return i


def test_preview_public_group():
    c = FakeClient(entities={"mygroup": _group()})
    r = asyncio.run(preview_public(c, "mygroup"))
    assert r.ok and r.dest_type == "supergroup" and r.title == "G"


def test_preview_public_user_and_bot_refused():
    c = FakeClient(entities={"someone": _user(), "mybot": _user(bot=True)})
    r1 = asyncio.run(preview_public(c, "someone"))
    assert r1.dest_type == "user"
    r2 = asyncio.run(preview_public(c, "mybot"))
    assert r2.dest_type == "bot"


def test_preview_invite_and_already():
    c = FakeClient(invite_result=_invite())
    r = asyncio.run(preview_invite(c, "HASH"))
    assert r.ok and r.dest_type == "private_invite"
    ch = _group()
    already = ChatInviteAlready.__new__(ChatInviteAlready)
    already.chat = ch
    c2 = FakeClient(invite_result=already)
    r2 = asyncio.run(preview_invite(c2, "HASH"))
    assert r2.dest_type == "already_member"


def test_preview_expired_invalid():
    c = FakeClient(invite_error=InviteHashExpiredError(request=None))
    assert asyncio.run(preview_invite(c, "H")).dest_type == "expired"
    c2 = FakeClient(invite_error=InviteHashInvalidError(request=None))
    assert asyncio.run(preview_invite(c2, "H")).dest_type == "invalid"


def test_join_then_confirm_success():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()})
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, "go https://t.me/mygroup"))
    assert "#1" in msg and "معاينة" in msg
    req = database.list_join_requests(db)[0]
    assert req["status"] == "pending_approval"
    ok = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "انضميت" in ok
    assert database.get_join_request(db, req["id"])["status"] == "joined"
    # second confirm refused (final state)
    again = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "مينفعش" in again


def test_join_user_creates_no_request():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"someone": _user()})
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, "!tg join https://t.me/someone"))
    assert "اتخطى" in msg
    assert database.list_join_requests(db) == []


def test_join_expired_creates_no_request_no_retry():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(invite_error=InviteHashExpiredError(request=None))
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/+ExpiredHash99"))
    assert "انتهت" in msg
    assert database.list_join_requests(db) == []


def test_confirm_approval_mode_reports_request_not_membership():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()},
                   join_error=InviteRequestSentError(request=None))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "الأدمن" in msg
    assert database.get_join_request(db, req["id"])["status"] == "join_requested"


def test_confirm_already_member():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()},
                   join_error=UserAlreadyParticipantError(request=None))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "عضو أصلًا" in msg
    assert database.get_join_request(db, req["id"])["status"] == "already_member"


def test_confirm_flood_stays_reconfirmable():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()}, flood_on="join")
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "انتظار" in msg
    st = database.get_join_request(db, req["id"])["status"]
    assert st == "failed_flood_deferred"
    # still confirmable later (server wait over + flood cleared)
    sm.flood_until = 0
    c.flood_on = None
    msg2 = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "انضميت" in msg2


def test_cooldown_blocks_rapid_confirms():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"a_group": _group("A", "a_group"), "b_group": _group("B", "b_group")})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/a_group"))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/b_group"))
    ids = [r["id"] for r in database.list_join_requests(db, status="pending_approval")]
    assert len(ids) == 2
    m1 = asyncio.run(join_manager.handle_confirm(db, c, sm, ids[0], cooldown_seconds=600))
    assert "انضميت" in m1
    m2 = asyncio.run(join_manager.handle_confirm(db, c, sm, ids[1], cooldown_seconds=600))
    assert "استنى" in m2


def test_network_failure_mid_join_is_uncertain():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()}, join_error=OSError("net down"))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "مش مؤكدة" in msg
    assert database.get_join_request(db, req["id"])["status"] == "failed_uncertain"


def test_cancel_pending_and_refuse_cancel_final():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"mygroup": _group()})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    assert "اتلغى" in join_manager.handle_cancel(db, req["id"])
    assert "مش محتاج" in join_manager.handle_cancel(db, req["id"])


def test_collect_never_creates_join_requests():
    from link_collector import process_parsed_links
    from link_parser import parse_message_text
    db = _tmpdb()
    parsed = parse_message_text("a https://t.me/+AAA111bbb222 and https://t.me/mygroup")
    process_parsed_links(db, parsed, "1", "G", None, "s")
    assert database.count_pending_joins(db) == 0
    assert database.list_join_requests(db) == []


def test_recover_stale_joining_marks_uncertain():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"mygroup": _group()})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    database.set_join_status(db, req["id"], "joining", bump_attempts=True)
    stale = database.recover_stale_joining(db)
    assert len(stale) == 1
    assert database.get_join_request(db, req["id"])["status"] == "failed_uncertain"


def test_migration_v1_to_v2_keeps_links():
    import sqlite3
    d = Path(tempfile.mkdtemp()) / "mig.db"
    con = sqlite3.connect(str(d))
    con.execute("CREATE TABLE links(id INTEGER PRIMARY KEY, normalized_url TEXT UNIQUE, original_url TEXT, kind TEXT, status TEXT, first_seen TEXT, last_seen TEXT, discovery_count INTEGER DEFAULT 1, last_source_title TEXT, last_source_id TEXT, need_review INTEGER DEFAULT 1, last_notified_at TEXT, notify_count INTEGER DEFAULT 0)")
    con.execute("INSERT INTO links(normalized_url, original_url, kind, status, first_seen, last_seen) VALUES('https://t.me/old_group','https://t.me/old_group','public_username','public_link_needs_review','x','y')")
    con.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO app_settings VALUES('schema_version','1')")
    con.commit()
    con.close()
    database.init_db(d)
    assert database.get_link(d, "https://t.me/old_group") is not None
    assert database.get_setting(d, "schema_version") == "2"
    r = database.create_join_request(d, "https://t.me/x", "public_username", "X", "supergroup", "d")
    assert r["status"] == "pending_approval"


def test_parse_command_variants():
    from main import parse_command
    assert parse_command("!tg join https://t.me/mygroup") == ("join", "https://t.me/mygroup")
    assert parse_command("!tg join<https://t.me/algeriadating>") == ("join", "https://t.me/algeriadating")
    assert parse_command("!tg join:https://t.me/mygroup") == ("join", "https://t.me/mygroup")
    assert parse_command("!tg   pending") == ("pending", "")
    assert parse_command("!tg confirm_join 3") == ("confirm_join", "3")
    assert parse_command("!tg cancel_join <4>") == ("cancel_join", "4")
    assert parse_command("!tg status") == ("status", "")
    assert parse_command("!tg help") == ("help", "")
    assert parse_command("!tg whatever") == ("help", "")
    assert parse_command("hello") == ("", "")  # bare unknown -> silent
    # Arabic aliases
    assert parse_command("!tg انضم https://t.me/mygroup") == ("join", "https://t.me/mygroup")
    assert parse_command("!tg انضم<https://t.me/mygroup>") == ("join", "https://t.me/mygroup")
    assert parse_command("!tg حالة") == ("status", "")
    assert parse_command("!tg الحاله") == ("status", "")
    assert parse_command("!tg المعلقة") == ("pending", "")
    assert parse_command("!tg تأكيد 3") == ("confirm_join", "3")
    assert parse_command("!tg الغاء 3") == ("cancel_join", "3")
    assert parse_command("!tg تلقائي on") == ("auto", "on")
    assert parse_command("!tg الفاصل 10") == ("interval", "10")
    assert parse_command("!tg الحد 10") == ("maxday", "10")
    assert parse_command("!tg تجميع on") == ("autocollect", "on")
    assert parse_command("!tg مساعدة") == ("help", "")


def test_parse_command_without_prefix_and_silent_text():
    from main import parse_command
    assert parse_command("انضم https://t.me/mygroup") == ("join", "https://t.me/mygroup")
    assert parse_command("حالة") == ("status", "")
    assert parse_command("معلقة") == ("pending", "")
    assert parse_command("أكد 3") == ("confirm_join", "3")
    assert parse_command("الغي 3") == ("cancel_join", "3")
    assert parse_command("تلقائي شغل") == ("auto", "شغل")
    assert parse_command("وقت 5") == ("interval", "5")
    assert parse_command("join https://t.me/mygroup") == ("join", "https://t.me/mygroup")
    # ordinary saved notes -> silent, no reply
    assert parse_command("حالة الطقس النهاردة") == ("status", "الطقس النهاردة")
    assert parse_command("فكرني اشتري عيش") == ("", "")
    assert parse_command("") == ("", "")


def test_interval_floor_one_with_warning():
    db = _tmpdb()
    ok, msg = join_manager.set_interval_minutes(db, 1)
    assert ok and "1 دقيقة" in msg and "تحذير" in msg
    ok2, _ = join_manager.set_interval_minutes(db, 0)
    assert not ok2
    ok3, msg3 = join_manager.set_interval_minutes(db, 30)
    assert ok3 and "تحذير" not in msg3


def test_status_text_is_arabic():
    from link_collector import build_status_text
    t = build_status_text(True, True, True, {}, {"unique_links": 1, "pending_review": 0},
                          pending_joins=0, auto_state={"enabled": False, "interval_min": 30,
                                                       "max_per_day": 20, "today_count": 0,
                                                       "autocollect": False})
    assert "حالة البوت" in t and "التوثيق" in t and "طلبات معلقة" in t
    assert "Auth" not in t and "Pending joins" not in t


def test_join_command_with_bracketed_url():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"algeriadating": _group("DZ", "algeriadating")})
    from main import parse_command
    cmd, arg = parse_command("!tg join<https://t.me/algeriadating>")
    assert cmd == "join"
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, arg))
    assert "#1" in msg


def test_bulk_join_creates_many_pendings():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"g11aa": _group("A", "g11aa"), "g22bb": _group("B", "g22bb"),
                             "g33cc": _group("C", "g33cc")})
    msg = asyncio.run(join_manager.handle_join_command(
        db, c, sm, "https://t.me/g11aa https://t.me/g22bb https://t.me/g33cc"))
    assert database.count_pending_joins(db) == 3
    assert "#1" in msg or "#2" in msg


def test_bulk_join_over_limit_refused():
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient()
    links = " ".join(f"https://t.me/g{i:02d}abc" for i in range(12))
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, links))
    assert "الحد" in msg
    assert database.count_pending_joins(db) == 0


def test_daily_cap_blocks_confirm():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    import database as _db
    _db.set_setting(db, "join_day", __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%d"))
    _db.set_setting(db, "join_day_count", "20")
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"], cooldown_seconds=0))
    assert "وصلت للحد اليومي" in msg
    assert database.get_join_request(db, req["id"])["status"] == "pending_approval"


def test_confirm_blocked_in_dry_run():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"mygroup": _group()})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/mygroup"))
    req = database.list_join_requests(db)[0]
    msg = asyncio.run(join_manager.handle_confirm(db, c, sm, req["id"],
                                                  cooldown_seconds=0, live=False))
    assert "التجربة" in msg
    assert database.get_join_request(db, req["id"])["status"] == "pending_approval"


def test_auto_disabled_or_empty_returns_none():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient()
    assert asyncio.run(join_manager.process_auto_queue(db, c, sm)) is None
    join_manager.set_auto_enabled(db, True)
    assert asyncio.run(join_manager.process_auto_queue(db, c, sm)) is None


def test_auto_processes_oldest_pending():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(entities={"g11aa": _group("A", "g11aa"), "g22bb": _group("B", "g22bb")})
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/g11aa"))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/g22bb"))
    join_manager.set_auto_enabled(db, True)
    report = asyncio.run(join_manager.process_auto_queue(db, c, sm, cooldown_seconds=0))
    assert report is not None and "تلقائي" in report
    first = database.get_join_request(db, 1)
    assert first["status"] == "joined"
    # interval gate: immediate second step does nothing
    assert asyncio.run(join_manager.process_auto_queue(db, c, sm, cooldown_seconds=0)) is None


def test_interval_validation():
    db = _tmpdb()
    ok, _ = join_manager.set_interval_minutes(db, 30)
    assert ok
    ok0, _ = join_manager.set_interval_minutes(db, 0)
    assert not ok0
    ok3, _ = join_manager.set_interval_minutes(db, 2000)
    assert not ok3


def test_preview_folder_lists_chats():
    from link_verifier import preview_folder
    c = FakeClient(folder_result=_folder_invite("Pack", 2))
    r = asyncio.run(preview_folder(c, "SLUG"))
    assert r.ok and r.dest_type == "folder_invite"
    assert "2" in r.detail and "Pack" in r.detail


def test_join_folder_then_confirm_joins_all():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(folder_result=_folder_invite("Pack", 2))
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, "folder https://t.me/addlist/SLUG123"))
    assert "#1" in msg and "folder_invite" in msg
    req = database.get_join_request(db, 1)
    assert req["status"] == "pending_approval" and req["kind"] == "folder"
    out = asyncio.run(join_manager.handle_confirm(db, c, sm, 1, cooldown_seconds=0))
    assert "2 شات" in out
    assert database.get_join_request(db, 1)["status"] == "joined"
    assert join_manager._today_count(db) == 2  # each chat counted


def test_folder_already_all_joined():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(folder_result=_folder_already(missing=0))
    msg = asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/addlist/DONE99"))
    assert "أصلًا" in msg
    assert database.list_join_requests(db) == []


def test_folder_cap_counts_each_chat():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(folder_result=_folder_invite("Big", 3))
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/addlist/BIG11"))
    out = asyncio.run(join_manager.handle_confirm(db, c, sm, 1, cooldown_seconds=0, max_per_day=2))
    assert "الحد اليومي" in out or "هيعدي الحد" in out
    assert database.get_join_request(db, 1)["status"] == "pending_approval"


def test_folder_flood_join_stays_reconfirmable():
    db = _tmpdb()
    sm = SafetyManager()
    sm.joining_paused = False
    c = FakeClient(folder_result=_folder_invite("P", 1), flood_on="folderjoin")
    asyncio.run(join_manager.handle_join_command(db, c, sm, "https://t.me/addlist/FLOOD1"))
    out = asyncio.run(join_manager.handle_confirm(db, c, sm, 1, cooldown_seconds=0))
    assert "انتظار" in out
    assert database.get_join_request(db, 1)["status"] == "failed_flood_deferred"


def test_autocollect_toggle_and_parse():
    from main import parse_command
    assert parse_command("!tg autocollect on") == ("autocollect", "on")
    assert parse_command("!tg autocollect off") == ("autocollect", "off")
    db = _tmpdb()
    assert not join_manager.is_autocollect_enabled(db)
    join_manager.set_autocollect(db, True)
    assert join_manager.is_autocollect_enabled(db)
    join_manager.set_autocollect(db, False)
    assert not join_manager.is_autocollect_enabled(db)


def test_auto_enqueue_new_only_group_candidates():
    from link_parser import parse_message_text
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(entities={"auto1": _group("A", "auto1"), "auto2": _group("B", "auto2")})
    parsed = parse_message_text("n https://t.me/auto1 and https://t.me/auto2 plus https://t.me/addstickers/zz")
    created, skipped = asyncio.run(join_manager.auto_enqueue_new(db, c, sm, parsed))
    assert created == 2
    assert database.count_pending_joins(db) == 2
    # same links again -> no duplicate pendings
    created2, _ = asyncio.run(join_manager.auto_enqueue_new(db, c, sm, parsed))
    assert created2 == 0
    assert database.count_pending_joins(db) == 2


def test_auto_enqueue_flood_is_graceful():
    from link_parser import parse_message_text
    db = _tmpdb()
    sm = SafetyManager()
    c = FakeClient(flood_on="entity")
    parsed = parse_message_text("n https://t.me/somegroup")
    created, skipped = asyncio.run(join_manager.auto_enqueue_new(db, c, sm, parsed))
    assert created == 0
    assert database.count_pending_joins(db) == 0
    assert sm.flood_wait_remaining() > 0

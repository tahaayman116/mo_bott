from helpers import chunk_text, extract_links, normalize_phone, parse_interval_minutes
from join_flow import classify_link


def test_normalize_phone():
    assert normalize_phone("+20 100 123 4567") == "+201001234567"
    assert normalize_phone("+201012345678") == "+201012345678"
    assert normalize_phone("abc") == ""
    assert normalize_phone("123") == ""
    assert normalize_phone("") == ""


def test_parse_interval_minutes():
    assert parse_interval_minutes("5") == 5
    assert parse_interval_minutes("5د") == 5
    assert parse_interval_minutes("كل 10 دقايق") == 10
    assert parse_interval_minutes("0") is None
    assert parse_interval_minutes("2000") is None
    assert parse_interval_minutes("مفيش") is None


def test_extract_links():
    text = "جرب https://t.me/abc وهنا https://t.me/+HASH1 و https://t.me/abc تاني"
    links = extract_links(text)
    assert links == ["https://t.me/abc", "https://t.me/+HASH1"]


def test_classify_link():
    assert classify_link("https://t.me/somegroup") == ("public", "somegroup")
    assert classify_link("https://t.me/+AbCdEf123456") == ("invite", "AbCdEf123456")
    assert classify_link("https://t.me/joinchat/AbCdEf") == ("invite", "AbCdEf")
    assert classify_link("https://t.me/addlist/AbCdEf") == ("folder", "AbCdEf")
    assert classify_link("https://t.me/somegroup/123") == ("unsupported", "https://t.me/somegroup/123")
    assert classify_link("https://google.com/x") == ("unsupported", "https://google.com/x")


def test_chunk_text():
    long = "سطر\n" * 2000
    parts = chunk_text(long)
    assert len(parts) > 1
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts) == long


def test_groups_selection_kb():
    from types import SimpleNamespace
    from bot import groups_kb, groups_text
    g = lambda t: SimpleNamespace(title=t)
    targets = [(g("G1"), True), (g("G2"), False), (g("G3"), True)]
    kb = groups_kb(targets, {0, 2})
    rows = kb.inline_keyboard
    assert rows[0][0].text.startswith("✅")  # G1 selected
    assert rows[1][0].text.startswith("⬜")  # G2 not
    assert rows[2][0].text.startswith("✅")  # G3 selected
    assert "🔇" in rows[1][0].text  # G2 not postable
    assert rows[0][0].callback_data == "p:t:0"
    assert "2 من 3" in groups_text(targets, {0, 2}, 5)


def test_list_post_targets_includes_all():
    import asyncio
    from telethon.tl.types import Channel, Chat, ChatAdminRights
    from broadcast_flow import _can_post, list_post_targets

    class FakeClient:
        def __init__(self, ents):
            self._ents = ents

        async def iter_dialogs(self):
            from types import SimpleNamespace
            for e in self._ents:
                yield SimpleNamespace(entity=e)

    chat = Chat(id=1, title="small", photo=None, participants_count=1,
                date=None, version=1)
    mega = Channel(id=2, title="mega", photo=None, date=None, broadcast=False,
                   megagroup=True)
    own_chan = Channel(id=3, title="mine", photo=None, date=None, broadcast=True,
                       megagroup=False, creator=True)
    admin_chan = Channel(
        id=4, title="admined", photo=None, date=None, broadcast=True,
        megagroup=False,
        admin_rights=ChatAdminRights(post_messages=True))
    plain_chan = Channel(id=5, title="readonly", photo=None, date=None,
                         broadcast=True, megagroup=False)
    ents = [chat, mega, own_chan, admin_chan, plain_chan]
    assert _can_post(chat) and _can_post(mega)
    assert _can_post(own_chan) and _can_post(admin_chan)
    assert not _can_post(plain_chan)

    targets = asyncio.run(list_post_targets(FakeClient(ents)))
    assert len(targets) == 5  # nothing hidden
    flags = [c for _, c in targets]
    assert flags == [True, True, True, True, False]


def test_describe_send_error():
    from broadcast_flow import describe_send_error
    assert describe_send_error(ValueError("X")) == "ValueError"
    slow = type("SlowModeWaitError", (Exception,), {})("flood")
    slow.seconds = 30
    assert "بطيء" in describe_send_error(slow)
    forb = type("ChatWriteForbiddenError", (Exception,), {})("x")
    assert "مقفول" in describe_send_error(forb)


def test_format_links_block():
    from collect_flow import format_links_block
    items = [{"kind": "جروب", "title": "A", "link": "https://t.me/a"},
             {"kind": "جروب", "title": "B", "link": ""},
             {"kind": "قناة", "title": "C", "link": "https://t.me/c"}]
    blocks = format_links_block(items)
    assert len(blocks) == 1
    assert "https://t.me/a" in blocks[0]
    assert "https://t.me/c" in blocks[0]
    assert blocks[0].startswith("```")
    assert format_links_block([{"link": ""}]) == []
    many = [{"link": f"https://t.me/g{i:04d}"} for i in range(500)]
    assert len(format_links_block(many)) > 1


def test_prepare_join_items_and_limit():
    from join_flow import (JOIN_LIMIT_MAX, clamp_join_limit,
                           prepare_join_items)
    assert clamp_join_limit(0) == 1
    assert clamp_join_limit(10) == 10
    assert clamp_join_limit(999) == JOIN_LIMIT_MAX
    items, folders = prepare_join_items([
        "https://t.me/somegroup",
        "https://t.me/+AbCdEf123456",
        "https://t.me/addlist/AbCdEf",
        "https://google.com/x",
    ])
    assert len(items) == 2
    assert folders == 1
    assert items[0][:2] == ("https://t.me/somegroup", "public")


def test_get_join_limit(tmp_path=None):
    import tempfile
    from pathlib import Path
    import database
    from bot import get_join_limit
    d = Path(tempfile.mkdtemp()) / "t.db"
    database.init_db(d)
    assert get_join_limit(d) == 10  # default
    database.kv_set(d, "join_limit", "25")
    assert get_join_limit(d) == 25
    database.kv_set(d, "join_limit", "junk")
    assert get_join_limit(d) == 10

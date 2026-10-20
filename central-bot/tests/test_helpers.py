from helpers import (chunk_text, extract_links, format_duration_ar,
                     normalize_phone, parse_interval_minutes,
                     parse_join_interval)
from join_flow import _entity_link, classify_link


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


def test_parse_join_interval():
    assert parse_join_interval("10s") == 10
    assert parse_join_interval("10 ثواني") == 10
    assert parse_join_interval("30 ثانية") == 30
    assert parse_join_interval("10 sec") == 10
    assert parse_join_interval("5") == 300
    assert parse_join_interval("5 دقايق") == 300
    assert parse_join_interval("1") == 60
    assert parse_join_interval("1440") == 86400
    assert parse_join_interval("5s") is None
    assert parse_join_interval("0") is None
    assert parse_join_interval("1441") is None
    assert parse_join_interval("86401s") is None
    assert parse_join_interval("مفيش") is None


def test_format_duration_ar():
    assert format_duration_ar(10) == "10 ثانية"
    assert format_duration_ar(60) == "دقيقة"
    assert format_duration_ar(120) == "دقيقتين"
    assert format_duration_ar(90) == "دقيقة و30 ثانية"
    assert format_duration_ar(300) == "5 دقايق"
    assert format_duration_ar(600) == "10 دقايق"
    assert format_duration_ar(1800) == "30 دقيقة"
    assert format_duration_ar(3600) == "ساعة"
    assert format_duration_ar(5400) == "ساعة و30 دقيقة"
    assert format_duration_ar(7200) == "ساعتين"
    assert format_duration_ar(10800) == "3 ساعات"
    assert format_duration_ar(86400) == "24 ساعة"


def test_entity_link():
    from types import SimpleNamespace
    assert _entity_link(None) == ""
    assert _entity_link(SimpleNamespace(username="")) == ""
    assert _entity_link(SimpleNamespace(username="mygroup")) == "https://t.me/mygroup"
    multi = SimpleNamespace(username=None, usernames=[
        SimpleNamespace(username="first"), SimpleNamespace(username="second")])
    assert _entity_link(multi) == "https://t.me/first"
    str_names = SimpleNamespace(username=None, usernames=["plain"])
    assert _entity_link(str_names) == "https://t.me/plain"


def test_interval_keyboard():
    from bot import interval_kb
    kb = interval_kb("j:iv")
    assert kb.inline_keyboard[0][0].callback_data == "j:ivs:10"
    assert kb.inline_keyboard[1][0].callback_data == "j:iv:1"
    assert kb.inline_keyboard[1][1].callback_data == "j:iv:2"
    assert kb.inline_keyboard[-1][0].callback_data == "x:cancel"
    skb = interval_kb("s:iv")
    assert skb.inline_keyboard[0][2].callback_data == "s:ivs:30"
    assert skb.inline_keyboard[1][1].callback_data == "s:iv:2"


def test_post_interval_keyboard():
    from bot import post_iv_kb
    kb = post_iv_kb()
    assert kb.inline_keyboard[0][0].callback_data == "p:iv:2"
    assert kb.inline_keyboard[0][2].callback_data == "p:iv:10"
    assert kb.inline_keyboard[-1][0].callback_data == "x:cancel"


def test_super_accounts_keyboard():
    from bot import super_accs_kb
    accs = [{"phone": "+20111", "display_name": "one"},
            {"phone": "+20222", "display_name": ""}]
    kb = super_accs_kb(accs, {"+20111"})
    assert kb.inline_keyboard[0][0].text.startswith("✅")
    assert kb.inline_keyboard[1][0].text.startswith("⬜")
    assert "one" in kb.inline_keyboard[0][0].text
    assert "+20222" in kb.inline_keyboard[1][0].text
    assert kb.inline_keyboard[0][0].callback_data == "s:acc:+20111"
    assert kb.inline_keyboard[2][0].callback_data == "s:all"
    assert kb.inline_keyboard[3][0].callback_data == "s:next"


def test_extract_links():
    text = "جرب https://t.me/abc وهنا https://t.me/+HASH1 و https://t.me/abc تاني"
    links = extract_links(text)
    assert links == ["https://t.me/abc", "https://t.me/+HASH1"]


def test_extract_all_links_watcher():
    from link_watcher import extract_all_links
    text = "لينك t.me/mygroup وكمان https://t.me/+HASH1 و t.me/addlist/Ab12"
    links = extract_all_links(text)
    assert "https://t.me/mygroup" in links
    assert "https://t.me/+HASH1" in links
    assert "https://t.me/addlist/Ab12" in links
    assert len(links) == len({u.lower() for u in links})


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


def test_folder_verification_returns_direct_links(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from telethon.tl import types
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.chatlists import (
        CheckChatlistInviteRequest,
        JoinChatlistInviteRequest,
    )
    from telethon.tl.functions.messages import (
        GetDialogFiltersRequest,
        GetFullChatRequest,
    )
    import join_flow

    async def no_sleep(_):
        return None

    monkeypatch.setattr(join_flow.asyncio, "sleep", no_sleep)

    pub = types.Channel(id=1, title="Public Group", photo=None, date=None,
                        access_hash=11, username="public_group", megagroup=True)
    priv = types.Chat(id=2, title="Private Group", photo=None,
                      participants_count=5, date=None, version=1)
    p1 = types.PeerChannel(1)
    p2 = types.PeerChat(2)
    invite = SimpleNamespace(peers=[p1, p2], chats=[pub, priv], users=[])
    remaining = SimpleNamespace(peers=[p1, p2], chats=[pub, priv], users=[])

    class FakeClient:
        def __init__(self):
            self.checks = 0

        async def __call__(self, req):
            if isinstance(req, CheckChatlistInviteRequest):
                self.checks += 1
                return invite if self.checks == 1 else remaining
            if isinstance(req, JoinChatlistInviteRequest):
                return None
            if isinstance(req, GetDialogFiltersRequest):
                return SimpleNamespace(filters=[])
            if isinstance(req, GetFullChatRequest):
                return SimpleNamespace(full_chat=SimpleNamespace(
                    exported_invite=SimpleNamespace(
                        link="https://t.me/+privInvite")))
            if isinstance(req, GetFullChannelRequest):
                raise AssertionError("public username should not need GetFull")
            raise AssertionError(type(req))

    res, manual = asyncio.run(
        join_flow.attempt_join(FakeClient(), "folder", "ABC"))
    assert "2" in res
    assert len(manual) == 2
    assert any("https://t.me/public_group" in m and "Public Group" in m
               for m in manual)
    assert any("https://t.me/+privInvite" in m and "Private Group" in m
               for m in manual)
    assert all("addlist" not in m for m in manual)


def test_super_worker_per_account_limit(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from telethon.tl.types import Chat
    import database
    from broadcast_flow import SuperBroadcastWorker

    db = tmp_path / "super.db"
    database.init_db(db)
    database.add_account(db, "+20111", "a.session", "one")
    database.add_account(db, "+20222", "b.session", "two")
    notes: list[str] = []
    sent: dict[str, list[str]] = {}

    class FakeClient:
        def __init__(self, phone):
            self.phone = phone

        async def iter_dialogs(self):
            for i in range(5):
                chat = Chat(id=i + 1, title=f"{self.phone}-{i}", photo=None,
                            participants_count=1, date=None, version=1)
                yield SimpleNamespace(entity=chat)

        async def send_message(self, ent, text):
            sent.setdefault(self.phone, []).append(ent.title)

    class FakeCtx:
        def __init__(self, phone):
            self.phone = phone

        async def __aenter__(self):
            return FakeClient(self.phone)

        async def __aexit__(self, *args):
            return False

    class FakeSM:
        def temp_client(self, phone):
            return FakeCtx(phone)

    async def notify(text):
        notes.append(text)

    async def main():
        w = SuperBroadcastWorker(db, FakeSM(), notify)
        w.start(["+20111", "+20222"], "ad", 10, 3)
        w.duration_sec = 0.03
        w.min_post_gap_sec = 0.001
        await w._task

    asyncio.run(main())
    assert len(sent["+20111"]) == 5
    assert len(sent["+20222"]) == 5
    assert database.counter_get(db, "sent:+20111") == 5
    assert database.counter_get(db, "sent:+20222") == 5
    assert database.counter_get(db, "posts") == 10
    report = "\n".join(notes)
    assert report.count("5 \u0645\u0646 5.") == 2
    assert "+20111-0" not in report
    assert "+20222-0" not in report


def test_super_duration_is_total_per_account(tmp_path):
    from broadcast_flow import SuperBroadcastWorker
    w = SuperBroadcastWorker(tmp_path / "x.db", None, None)
    w.duration_sec = 300
    w.batch_size = 20
    w.min_post_gap_sec = 10
    assert w._batch_gap(463) == 15
    assert w._batch_gap(5) == 60

    w.batch_size = 0
    assert w._batch_gap(10) == 30
    assert w._batch_gap(2000) == 10


def test_super_report_counts_success_and_lists_blocked(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from telethon.tl.types import Channel
    import database
    from broadcast_flow import SuperBroadcastWorker

    db = tmp_path / "super_report.db"
    database.init_db(db)
    database.add_account(db, "+20111", "a.session", "one")
    notes: list[str] = []

    class FakeClient:
        async def iter_dialogs(self):
            for cid, title, mega in ((1, "Good Group", True),
                                     (2, "Banned Group", True),
                                     (3, "Skip Channel", False)):
                ch = Channel(id=cid, title=title, photo=None, date=None,
                             access_hash=cid, megagroup=mega)
                yield SimpleNamespace(entity=ch)

        async def send_message(self, ent, text):
            if ent.id == 2:
                raise Exception("USER_BANNED_IN_CHANNEL")

    class FakeCtx:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, *args):
            return False

    class FakeSM:
        def temp_client(self, phone):
            return FakeCtx()

    async def notify(text):
        notes.append(text)

    async def main():
        w = SuperBroadcastWorker(db, FakeSM(), notify)
        w.start(["+20111"], "ad", 10, 0)
        w.duration_sec = 0.03
        w.min_post_gap_sec = 0.001
        await w._task

    asyncio.run(main())
    report = "\n".join(notes)
    assert "تم النشر بنجاح: 1 من 3" in report
    assert "Good Group" not in report
    assert "🚫 جروبات خد منها حظر/منع (1)" in report
    assert "• Banned Group — محظور من النشر" in report
    assert "⏭️ متخطى: 1" in report


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
    from join_flow import (JOIN_LIMIT_MAX, clamp_join_limit, merge_links,
                           prepare_join_items)
    assert clamp_join_limit(0) == 1
    assert clamp_join_limit(10) == 10
    assert clamp_join_limit(9999) == JOIN_LIMIT_MAX == 500
    assert merge_links(["https://t.me/a"], ["https://t.me/A", "https://t.me/b"]) == [
        "https://t.me/a", "https://t.me/b"]
    items, folders = prepare_join_items([
        "https://t.me/somegroup",
        "https://t.me/+AbCdEf123456",
        "https://t.me/addlist/AbCdEf",
        "https://google.com/x",
    ])
    assert len(items) == 3
    assert folders == 1
    assert items[0][:2] == ("https://t.me/somegroup", "public")
    assert items[2][:2] == ("https://t.me/addlist/AbCdEf", "folder")


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


def test_progress_callback_in_collect():
    import asyncio
    from collect_flow import collect_dialogs

    class FakeDlg:
        entity = None
    class FakeClient:
        async def iter_dialogs(self):
            for _ in range(120):
                yield FakeDlg()
    seen: list[int] = []
    async def on_prog(n):
        seen.append(n)
    items = asyncio.run(collect_dialogs(FakeClient(), on_progress=on_prog))
    assert items == []
    assert seen == [50, 100]

def test_big_list_preview_and_manual_rows():
    from bot import build_join_preview, build_manual_rows
    items = [(f"https://t.me/g{i:04d}", "public", f"g{i:04d}") for i in range(60)]
    text, kb = build_join_preview(items, 0)
    assert "60 لينك" in text
    assert "كمان" in text  # summarized, not 60 lines
    assert len(text) < 4096
    rows = build_manual_rows(items)
    ones = [r for r in rows if r[0].callback_data.startswith("j:one:")]
    assert len(ones) == 40  # Telegram-safe button count
    assert rows[-2][0].callback_data == "j:all"  # confirm-all covers everything
    assert rows[0][0].callback_data == "j:one:0"
    assert rows[40][0].callback_data == "j:noop"


def test_reset_flow_state_clears_pending_flow():
    import asyncio
    from types import SimpleNamespace
    import bot

    class FakeConv:
        def __init__(self):
            self._conversations = {("chat", "user"): ("x", 1)}

        def _get_key(self, update):
            return ("chat", "user")

    class FakeSM:
        def __init__(self):
            self.cancelled = []

        async def cancel_login(self, phone):
            self.cancelled.append(phone)

    sm = FakeSM()
    conv = FakeConv()
    user_data = {"add_phone": "+201000000000", "code_tries": 1,
                 "ad_text": "ad", "post_targets": [1], "post_sel": {0},
                 "join_links_all": ["x"], "join_items": [("keep",)],
                 "super_accs": {"+2011"}, "super_text": "ad",
                 "super_iv": 10, "super_limit": 5}
    context = SimpleNamespace(user_data=user_data,
                              bot_data={"sm": sm, "convs": [conv]})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
    )
    old = bot.OWNER
    bot.OWNER = 1
    try:
        asyncio.run(bot.reset_flow_state(update, context))
    finally:
        bot.OWNER = old
    assert sm.cancelled == ["+201000000000"]
    assert conv._conversations == {}
    assert "ad_text" not in user_data
    assert "post_targets" not in user_data
    assert "super_accs" not in user_data
    assert "super_text" not in user_data
    assert "super_iv" not in user_data
    assert "super_limit" not in user_data
    assert user_data["join_items"] == [("keep",)]


def test_build_app_wires_all_handlers(tmp_path):
    from types import SimpleNamespace
    import bot

    cfg = SimpleNamespace(
        owner_id=1,
        db_path=tmp_path / "bot.db",
        post_daily_cap=100,
        bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    )
    old = bot.OWNER
    try:
        app = bot.build_app(cfg, SimpleNamespace())
    finally:
        bot.OWNER = old
    assert app.bot_data["superbc"] is not None
    assert len(app.bot_data["convs"]) == 5

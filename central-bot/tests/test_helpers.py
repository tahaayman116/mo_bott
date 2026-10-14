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
    targets = [SimpleNamespace(title="G1"), SimpleNamespace(title="G2"),
               SimpleNamespace(title="G3")]
    kb = groups_kb(targets, {0, 2})
    rows = kb.inline_keyboard
    assert rows[0][0].text.startswith("✅")  # G1 selected
    assert rows[1][0].text.startswith("⬜")  # G2 not
    assert rows[2][0].text.startswith("✅")  # G3 selected
    assert rows[0][0].callback_data == "p:t:0"
    assert "2 من 3" in groups_text(targets, {0, 2}, 5)


def test_describe_send_error():
    from broadcast_flow import describe_send_error
    assert describe_send_error(ValueError("X")) == "ValueError"
    slow = type("SlowModeWaitError", (Exception,), {})("flood")
    slow.seconds = 30
    assert "بطيء" in describe_send_error(slow)
    forb = type("ChatWriteForbiddenError", (Exception,), {})("x")
    assert "مقفول" in describe_send_error(forb)

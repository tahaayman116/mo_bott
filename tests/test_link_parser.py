"""Parser tests: no Telegram account needed. Mocks only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from link_parser import (
    classify,
    is_group_candidate,
    normalize_url,
    parse_message_text,
    redact_url_for_logs,
)


def test_plus_hash():
    out = parse_message_text("join us https://t.me/+AbC123XyZ456")
    assert len(out) == 1
    assert out[0].kind == "private_invite"
    assert out[0].normalized_url == "https://t.me/+AbC123XyZ456"


def test_joinchat():
    out = parse_message_text("see https://t.me/joinchat/AAAAAEkk2WdoDrB4-Q8-gg ok")
    assert len(out) == 1
    assert out[0].kind == "private_invite"


def test_public_username():
    out = parse_message_text("visit https://t.me/example_group now")
    assert len(out) == 1
    assert out[0].kind == "public_username"
    assert out[0].status == "public_link_needs_review"


def test_multiple_links_one_message():
    out = parse_message_text("a https://t.me/+AAA111bbb222 and b https://t.me/some_group end")
    norms = {o.normalized_url for o in out}
    assert "https://t.me/+AAA111bbb222" in norms
    assert "https://t.me/some_group" in norms
    assert len(out) == 2


def test_caption_like_text():
    # captions are passed as text by the collector; same function covers them
    out = parse_message_text("caption here:\nhttps://t.me/joinchat/ZZZ123abcXYZ.")
    assert len(out) == 1
    assert out[0].kind == "private_invite"


def test_trailing_punctuation_and_normalization():
    out = parse_message_text("(see https://t.me/example_group, now!)")
    assert len(out) == 1
    assert out[0].normalized_url == "https://t.me/example_group"
    # equivalent forms dedup to one normalized url
    a = normalize_url("http://www.telegram.me/example_group/")
    b = normalize_url("https://t.me/example_group")
    assert a == b == "https://t.me/example_group"
    # hash case preserved
    assert normalize_url("https://T.ME/+AbC123") == "https://t.me/+AbC123"


def test_http_www_caps_variants():
    out = parse_message_text("try HTTP://WWW.T.ME/+XyZ999aaaBBB.")
    assert len(out) == 1
    assert out[0].normalized_url == "https://t.me/+XyZ999aaaBBB"


def test_non_telegram_ignored():
    out = parse_message_text("visit https://example.com/t.me/fake and youtube.com")
    assert out == []


def test_ordinary_mention_not_link():
    assert parse_message_text("I love Telegram very much") == []
    assert parse_message_text("t.me is cool") == []


def test_non_group_destination_labelled():
    out = parse_message_text("sticker https://t.me/addstickers/mypack")
    assert len(out) == 1
    assert out[0].kind == "other_telegram"
    assert not is_group_candidate(out[0])


def test_entity_hidden_url():
    out = parse_message_text("click here", entity_urls=["https://t.me/+HiddenHash12345"])
    assert len(out) == 1
    assert out[0].kind == "private_invite"


def test_unverified_private_kept():
    out = parse_message_text("mystery https://t.me/+ZZZ999YYY888")
    assert out[0].status == "private_invite_unverified"


def test_folder_link_optional_not_group_main():
    out = parse_message_text("folder https://t.me/addlist/ABCDEF123456")
    assert len(out) == 1
    assert out[0].kind == "folder"


def test_redact_logs_hides_hash():
    r = redact_url_for_logs("https://t.me/+AbC123XyZ456")
    assert "+AbC123XyZ456" not in r
    assert "t.me" in r


def test_extract_invite_hash_and_username():
    from link_parser import extract_invite_hash, extract_username
    assert extract_invite_hash("https://t.me/+AbC123XyZ") == "AbC123XyZ"
    assert extract_invite_hash("https://t.me/joinchat/AAAAAEkk2WdoDrB4-Q8-gg") == "AAAAAEkk2WdoDrB4-Q8-gg"
    assert extract_invite_hash("https://t.me/some_group") is None
    assert extract_username("https://t.me/some_group") == "some_group"
    assert extract_username("https://t.me/s/some_group") == "some_group"
    assert extract_username("https://t.me/+AbC123") is None
    assert extract_username("https://t.me/addstickers/x") is None


def test_folder_link_detected_and_is_candidate():
    from link_parser import extract_folder_slug, is_group_candidate
    out = parse_message_text("packs https://t.me/addlist/AbCdEf123456 here")
    assert len(out) == 1
    assert out[0].kind == "folder"
    assert is_group_candidate(out[0])
    assert extract_folder_slug("https://t.me/addlist/AbCdEf123456") == "AbCdEf123456"
    assert extract_folder_slug("https://t.me/addlist") is None

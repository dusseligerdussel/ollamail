"""Unit tests for the IMAP wire format (no server needed)."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.mail.providers.imap_protocol import (
    Continuation,
    ImapParseError,
    ResponseCode,
    Tagged,
    UidSet,
    Untagged,
    decode_mailbox,
    encode_keyword,
    encode_mailbox,
    fetch_items,
    flags_from_imap,
    flags_to_imap,
    format_search_date,
    literal_size,
    parse_internaldate,
    parse_response,
    quote,
)


def test_fetch_with_literal_and_bracketed_items() -> None:
    source = b"Subject: test\r\n\r\nline (with parens) and {braces}\r\n"
    response = parse_response(
        [
            b"* 12 FETCH (UID 42 FLAGS (\\Seen $Forwarded) MODSEQ (7) "
            b'INTERNALDATE "17-Jul-1996 02:44:25 -0700" '
            b"BODY[HEADER.FIELDS (MESSAGE-ID)] {%d}" % len(source),
            source,
            b" BODY[] NIL)",
        ]
    )

    assert isinstance(response, Untagged)
    assert (response.kind, response.number) == ("FETCH", 12)
    items = fetch_items(response)
    assert items["UID"] == "42"
    assert items["FLAGS"] == ["\\Seen", "$Forwarded"]
    assert items["MODSEQ"] == ["7"]
    assert items["BODY[HEADER.FIELDS (MESSAGE-ID)]"] == source
    assert items["BODY[]"] is None


def test_partial_body_key_is_normalised() -> None:
    response = parse_response([b"* 1 FETCH (BODY[]<0> {3}", b"abc", b")"])
    assert isinstance(response, Untagged)
    assert fetch_items(response)["BODY[]"] == b"abc"


def test_status_responses_and_codes() -> None:
    assert parse_response([b"* OK [UIDVALIDITY 3857529045] UIDs valid"]) == Untagged(
        "OK", code=ResponseCode("UIDVALIDITY", ("3857529045",))
    )
    flags = parse_response([b"* OK [PERMANENTFLAGS (\\Deleted \\Seen \\*)] Limited"])
    assert isinstance(flags, Untagged) and flags.code is not None
    assert flags.code.args == (["\\Deleted", "\\Seen", "\\*"],)

    tagged = parse_response([b"A0003 OK [COPYUID 38505 304,319:320 3956:3958] Done"])
    assert tagged == Tagged(
        "A0003", "OK", ResponseCode("COPYUID", ("38505", "304,319:320", "3956:3958"))
    )
    assert parse_response([b"A1 NO [AUTHENTICATIONFAILED] Authentication failed."]) == Tagged(
        "A1", "NO", ResponseCode("AUTHENTICATIONFAILED")
    )
    # Free text in unknown codes does not break parsing.
    odd = parse_response([b'* NO [WEIRD "unterminated] text'])
    assert isinstance(odd, Untagged) and odd.code is not None and odd.code.name == "WEIRD"
    assert parse_response([b"+ idling"]) == Continuation(b"idling")


def test_list_search_esearch_and_vanished() -> None:
    listing = parse_response([b'* LIST (\\HasNoChildren \\Sent) "/" "Sent Items"'])
    assert isinstance(listing, Untagged)
    assert listing.values == (["\\HasNoChildren", "\\Sent"], b"/", b"Sent Items")
    nil_delimiter = parse_response([b"* LIST () NIL INBOX"])
    assert isinstance(nil_delimiter, Untagged) and nil_delimiter.values == ([], None, "INBOX")
    literal_name = parse_response([b'* LIST () "/" {5}', b"a b c", b""])
    assert isinstance(literal_name, Untagged) and literal_name.values[2] == b"a b c"

    assert parse_response([b"* SEARCH 2 84 882"]) == Untagged("SEARCH", values=("2", "84", "882"))
    esearch = parse_response([b'* ESEARCH (TAG "A5") UID ALL 1:3,7'])
    assert isinstance(esearch, Untagged)
    assert esearch.values == (["TAG", b"A5"], "UID", "ALL", "1:3,7")
    assert parse_response([b"* VANISHED (EARLIER) 41,43:116"]) == Untagged(
        "VANISHED", values=(["EARLIER"], "41,43:116")
    )
    assert parse_response([b"* 23 EXISTS"]) == Untagged("EXISTS", 23)
    # Unknown responses are not parsed (they may contain free text).
    assert parse_response([b'* NAMESPACE (("" "/")) NIL NIL']) == Untagged("NAMESPACE")


def test_quoted_strings_unescape() -> None:
    response = parse_response([b'* LIST () "/" "a \\"quoted\\" \\\\ name"'])
    assert isinstance(response, Untagged)
    assert response.values[2] == b'a "quoted" \\ name'


@pytest.mark.parametrize(
    "segments",
    [
        [b"* 1 FETCH (UID 1"],
        [b'* LIST () "/" "unterminated'],
        [b"* 1 FETCH (BODY[] {3} x", b"abc", b")"],
        [b"A1 MAYBE done"],
        [b"* 1 FETCH (UID 1)", b"extra"],
    ],
)
def test_invalid_responses_raise(segments: list[bytes]) -> None:
    with pytest.raises(ImapParseError):
        response = parse_response(segments)
        if isinstance(response, Untagged) and response.kind == "FETCH":
            fetch_items(response)


def test_literal_size() -> None:
    assert literal_size(b"* 1 FETCH (BODY[] {123}") == 123
    assert literal_size(b"A1 APPEND INBOX {5+}") == 5
    assert literal_size(b'* OK "text {5}"') is None


def test_quote_and_literal_decision() -> None:
    assert quote(b'pa"ss\\word') == b'"pa\\"ss\\\\word"'
    assert quote(b"") == b'""'
    assert quote("pässword".encode()) is None
    assert quote(b"line\r\nbreak") is None


@pytest.mark.parametrize(
    ("name", "wire"),
    [
        ("INBOX", b"INBOX"),
        ("Entwürfe", b"Entw&APw-rfe"),
        ("Tom & Jerry", b"Tom &- Jerry"),
        ("日本語", b"&ZeVnLIqe-"),
        ("Gelöschte Elemente/Ä", b"Gel&APY-schte Elemente/&AMQ-"),
    ],
)
def test_modified_utf7_roundtrip(name: str, wire: bytes) -> None:
    assert encode_mailbox(name) == wire
    assert decode_mailbox(wire) == name


def test_mailbox_decoding_is_lenient() -> None:
    assert decode_mailbox("Entwürfe".encode()) == "Entwürfe"
    assert decode_mailbox(b"broken &AP") == "broken &AP"


def test_uid_set() -> None:
    uids = UidSet.of([5, 1, 2, 3, 9, 10, 2])
    assert str(uids) == "1:3,5,9:10"
    assert list(uids) == [1, 2, 3, 5, 9, 10]
    assert len(uids) == 6
    assert (uids.min, uids.max) == (1, 10)
    assert 5 in uids and 4 not in uids and 11 not in uids
    assert UidSet.parse(str(uids)) == uids
    assert UidSet.parse("7:3") == UidSet.parse("3:7")
    assert str(uids.union([4, 11])) == "1:5,9:11"
    assert str(uids.difference([2, 9])) == "1,3,5,10"
    assert not UidSet.parse("")
    for invalid in ("1:*", "a", "0:2"):
        with pytest.raises(ImapParseError):
            UidSet.parse(invalid)


def test_large_ranges_stay_compact() -> None:
    uids = UidSet.parse("1:4000000000")
    assert 3999999999 in uids
    assert str(uids.union(UidSet.parse("4000000001"))) == "1:4000000001"


def test_flag_mapping() -> None:
    flags = flags_from_imap(["\\Seen", "\\ANSWERED", "\\Recent", "\\Unknown", "$Label1", b"x"])
    assert flags == {"seen", "answered", "$Label1"}
    assert flags_to_imap({"seen", "flagged", "deleted", "draft", "$Label1"}) == [
        "$Label1",
        "\\Deleted",
        "\\Draft",
        "\\Flagged",
        "\\Seen",
    ]


def test_keywords_are_valid_atoms_and_deterministic() -> None:
    assert encode_keyword("Wichtig") == "Wichtig"
    assert encode_keyword("Warten auf") == "Warten_auf"
    assert encode_keyword("Prüfen") == "Pr&APw-fen"
    assert encode_keyword("a(b)c*%") == "abc"
    assert flags_to_imap({"Warten auf"}) == ["Warten_auf"]
    with pytest.raises(ValueError):
        encode_keyword("()")


def test_dates() -> None:
    assert format_search_date(date(1994, 2, 1)) == "1-Feb-1994"
    parsed = parse_internaldate("17-Jul-1996 02:44:25 -0700")
    assert parsed == datetime(1996, 7, 17, 2, 44, 25, tzinfo=timezone(-timedelta(hours=7)))
    assert parse_internaldate(b" 1-Jan-2026 00:00:00 +0000") == datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_internaldate("31-Feb-2026 00:00:00 +0000") is None
    assert parse_internaldate("garbage") is None
    assert parse_internaldate(None) is None

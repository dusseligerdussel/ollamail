"""Reply composition: subject, recipients, threading headers, MIME source."""

from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default

import pytest

from app.mail import compose
from app.mail.providers.base import OutgoingAddress

ERIKA = {"name": "Erika Mustermann", "address": "erika@example.org"}
MAX = {"name": "Max Mustermann", "address": "max@example.org"}
TEAM = {"name": None, "address": "team@example.org"}
OWN = "erika@example.org"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Angebot", "Re: Angebot"),
        ("Re: Angebot", "Re: Angebot"),
        ("AW: Re: Angebot", "Re: Angebot"),
        ("Antw: SV:  Angebot", "Re: Angebot"),
        ("RE[2]: Angebot", "Re: Angebot"),
        ("Fwd: Angebot", "Re: Fwd: Angebot"),
        ("", "Re:"),
    ],
)
def test_reply_subject(subject: str, expected: str) -> None:
    assert compose.reply_subject(subject) == expected


def addresses(values: list[OutgoingAddress]) -> list[str]:
    return [a.address for a in values]


def test_reply_goes_to_the_sender() -> None:
    to, cc = compose.reply_recipients(
        sender=MAX, to=[ERIKA, TEAM], cc=[], reply_to=[], own_addresses=[OWN], reply_all=False
    )
    assert (addresses(to), cc) == (["max@example.org"], [])
    assert to[0].name == "Max Mustermann"


def test_reply_to_header_wins() -> None:
    to, _ = compose.reply_recipients(
        sender=MAX,
        to=[ERIKA],
        cc=[],
        reply_to=[{"name": "Support", "address": "support@example.org"}],
        own_addresses=[OWN],
        reply_all=False,
    )
    assert addresses(to) == ["support@example.org"]


def test_reply_all_copies_the_others_but_not_the_own_address() -> None:
    to, cc = compose.reply_recipients(
        sender=MAX,
        to=[{"name": None, "address": "Erika@Example.org"}, TEAM],
        cc=[MAX, {"name": "Lena", "address": "lena@example.org"}],
        reply_to=[],
        own_addresses=[OWN],
        reply_all=True,
    )
    assert addresses(to) == ["max@example.org"]
    assert addresses(cc) == ["team@example.org", "lena@example.org"]


def test_reply_to_an_own_mail_goes_to_its_recipients() -> None:
    to, cc = compose.reply_recipients(
        sender=ERIKA, to=[MAX], cc=[TEAM], reply_to=[], own_addresses=[OWN], reply_all=True
    )
    assert (addresses(to), addresses(cc)) == (["max@example.org"], ["team@example.org"])


def test_note_to_self_keeps_the_own_address() -> None:
    to, _ = compose.reply_recipients(
        sender=ERIKA, to=[ERIKA], cc=[], reply_to=[], own_addresses=[OWN], reply_all=False
    )
    assert addresses(to) == [OWN]


def test_references_append_the_message_id_and_stay_short() -> None:
    assert compose.references("<b@x>", ["<a@x>"]) == ["<a@x>", "<b@x>"]
    assert compose.references("<b@x>", ["<a@x>", "<b@x>"]) == ["<a@x>", "<b@x>"]
    assert compose.references(None, []) == []
    long = [f"<{n}@x>" for n in range(30)]
    chain = compose.references("<new@x>", long)
    assert len(chain) == compose.MAX_REFERENCES
    assert chain[0] == "<0@x>"
    assert chain[-1] == "<new@x>"


def build(**overrides: object) -> bytes:
    values: dict[str, object] = {
        "sender": OutgoingAddress(OWN, "Erika Müller"),
        "to": [OutgoingAddress("max@example.org", "Max Mustermann")],
        "cc": [OutgoingAddress("team@example.org")],
        "subject": "Re: Übergabe",
        "body": "Hallo Max,\n\nbis Freitag.\n\nErika",
        "in_reply_to": "orig@example.org",
        "references": ["<root@example.org>", "<orig@example.org>"],
        "date": datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    raw, _ = compose.build_reply(**values)  # type: ignore[arg-type]
    return raw


def test_build_reply_headers_and_body() -> None:
    raw, message_id = compose.build_reply(
        sender=OutgoingAddress(OWN, "Erika Müller"),
        to=[OutgoingAddress("max@example.org", "Max Mustermann")],
        cc=[],
        subject="Re: Übergabe",
        body="Grüße",
        in_reply_to="<orig@example.org>",
        references=["<orig@example.org>"],
        date=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
    )
    parsed = message_from_bytes(raw, policy=default)

    assert parsed["Message-ID"] == message_id
    assert message_id.endswith("@example.org>")
    assert parsed["From"] == "Erika Müller <erika@example.org>"
    assert parsed["To"] == "Max Mustermann <max@example.org>"
    assert parsed["Cc"] is None
    assert parsed["Subject"] == "Re: Übergabe"
    assert parsed["In-Reply-To"] == "<orig@example.org>"
    assert parsed["References"] == "<orig@example.org>"
    assert parsed.get_content_type() == "text/plain"
    assert parsed.get_content().strip() == "Grüße"
    assert all(len(line) <= 998 for line in raw.split(b"\r\n"))


def test_build_reply_brackets_bare_message_ids() -> None:
    parsed = message_from_bytes(build(), policy=default)
    assert parsed["In-Reply-To"] == "<orig@example.org>"
    assert parsed["References"] == "<root@example.org> <orig@example.org>"
    assert parsed["Cc"] == "team@example.org"


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": [OutgoingAddress("max@example.org\r\nBcc: spy@example.org")]},
        {"to": [OutgoingAddress("max@example.org", "Max\nBcc: spy@example.org")]},
        {"to": [OutgoingAddress("not an address")]},
        {"subject": "Re: x\r\nBcc: spy@example.org"},
    ],
)
def test_header_injection_is_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(compose.InvalidAddressError):
        build(**overrides)


def test_quote() -> None:
    quoted = compose.quote(
        "Hallo Erika,\n\nkommst du?\n",
        sender="Max Mustermann",
        sent_at=datetime(2026, 10, 1, 9, 30, tzinfo=UTC),
        language="de",
    )
    assert quoted == (
        "Am 2026-10-01 09:30 schrieb Max Mustermann:\n> Hallo Erika,\n>\n> kommst du?"
    )
    english = compose.quote("x", sender="Max", sent_at=None, language="fr")
    assert english == "On ?, Max wrote:\n> x"

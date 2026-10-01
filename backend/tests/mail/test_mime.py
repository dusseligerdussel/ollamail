from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.mail.mime import Address, decode_bytes, normalize_message, parse_message
from tests.mail.conftest import fixture_names, load_fixture


@dataclass(frozen=True)
class Expected:
    subject: str
    sender: str | None
    language: str | None
    main_contains: str
    main_excludes: tuple[str, ...] = ()
    quoted: bool = False
    signature: str | None = None
    attachments: tuple[str | None, ...] = ()


EXPECTED = {
    "01-plain-ascii.eml": Expected(
        "Meeting tomorrow", "max.mustermann@example.com", "en", "move our meeting"
    ),
    "02-outlook-reply-cp1252.eml": Expected(
        "AW: Unterlagen Projekt Nordlicht",
        "erika@example.org",
        "de",
        "überarbeitete Kostenübersicht",
        main_excludes=("Gesendet:", "anbei die Unterlagen"),
        quoted=True,
    ),
    "03-gmail-reply-utf8.eml": Expected(
        "Re: Workshop am Donnerstag",
        "alex.beispiel@example.net",
        "de",
        "Können wir uns um 10 Uhr",
        main_excludes=("schrieb", "wer hat Zeit"),
        quoted=True,
    ),
    "04-newsletter-html.eml": Expected(
        "Product news in September",
        "newsletter@news.example.net",
        "en",
        "faster search",
        main_excludes=("document.write", "tracking", "MsoNormal"),
    ),
    "05-nested-multipart.eml": Expected(
        "Quarterly report Q3",
        "jordan@example.com",
        "en",
        "quarterly report attached",
        attachments=("logo.png", "report-q3.pdf"),
    ),
    "06-broken-charset-latin1-as-utf8.eml": Expected(
        "Präsentation Mittwoch", "bernd@example.com", "de", "Grüße aus Köln"
    ),
    "07-unknown-charset-rfc2047.eml": Expected(
        "Rechnung für September \u2013 Nr. 42", "juergen@example.com", "de", "Viele Grüße"
    ),
    "08-koi8r-russian.eml": Expected("Отчёт за квартал", "ivan@example.net", "ru", "отчёт"),
    "09-signature-delimiter.eml": Expected(
        "Server maintenance on Saturday",
        "sam@example.org",
        "en",
        "batch jobs are paused",
        main_excludes=("Operations Team",),
        signature="Sam Sample\nOperations Team\nExample Org, Sample Street 1, 12345 Sample City",
    ),
    "10-mobile-signature.eml": Expected(
        "Re: Meeting tomorrow",
        "erika@example.org",
        "de",
        "14 Uhr passt mir gut",
        main_excludes=("iPhone", "can we move"),
        quoted=True,
        signature="Von meinem iPhone gesendet",
    ),
    "11-forward-rfc822.eml": Expected(
        "Fwd: Offer for the new printers",
        "max.mustermann@example.com",
        "en",
        "attached offer",
        attachments=("attached-message.eml",),
    ),
    "12-broken-headers-rfc2231.eml": Expected(
        "Contract draft",
        "broken@example.com",
        "en",
        "review the contract draft",
        attachments=("Vertrag Übersicht \u2013 Entwurf.pdf", "passwd"),
    ),
    "13-reply-chain-references.eml": Expected(
        "AW: Re: Workshop am Donnerstag",
        "max.mustermann@example.com",
        "de",
        "Ich reserviere den Raum",
        main_excludes=("wrote", "klingt gut"),
        quoted=True,
    ),
    "14-iso2022jp-japanese.eml": Expected("会議資料", "yamada@example.jp", "ja", "会議の資料"),
}


def test_every_fixture_has_expectations() -> None:
    assert set(fixture_names()) == set(EXPECTED)
    assert len(EXPECTED) >= 10


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_normalisation(name: str) -> None:
    expected = EXPECTED[name]
    normalized = normalize_message(load_fixture(name))
    parsed = normalized.parsed

    assert parsed.subject == expected.subject
    assert (parsed.sender.address if parsed.sender else None) == expected.sender
    assert normalized.language == expected.language
    assert expected.main_contains in normalized.body_main
    for excluded in expected.main_excludes:
        assert excluded not in normalized.body_main
    assert (normalized.quoted is not None) == expected.quoted
    assert normalized.signature == expected.signature
    assert tuple(a.filename for a in parsed.attachments) == expected.attachments
    assert parsed.size == len(load_fixture(name))
    for text in (parsed.subject, normalized.body_text, *(v for _, v in parsed.headers)):
        assert "�" not in text
        assert "\x00" not in text


def test_reply_headers_and_references() -> None:
    parsed = parse_message(load_fixture("13-reply-chain-references.eml"))

    assert parsed.message_id == "workshop-0013@example.com"
    assert parsed.in_reply_to == "CAgmail-0003@mail.example.net"
    assert parsed.references == ("workshop-0001@example.com", "CAgmail-0003@mail.example.net")
    assert parsed.sent_at == datetime(2026, 9, 1, 6, 10, tzinfo=UTC)


def test_addresses_with_encoded_names() -> None:
    parsed = parse_message(load_fixture("07-unknown-charset-rfc2047.eml"))

    assert parsed.sender == Address("Jürgen Müller", "juergen@example.com")
    assert parsed.to == (Address("Erika Müstermann", "erika@example.org"),)


def test_cc_and_list_headers_are_kept() -> None:
    gmail = parse_message(load_fixture("03-gmail-reply-utf8.eml"))
    newsletter = parse_message(load_fixture("04-newsletter-html.eml"))

    assert gmail.cc == (Address(None, "team@example.com"),)
    headers = dict(newsletter.headers)
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert headers["Precedence"] == "bulk"


def test_html_only_mail_keeps_html_and_derives_text() -> None:
    normalized = normalize_message(load_fixture("04-newsletter-html.eml"))

    assert normalized.parsed.text is None
    assert normalized.parsed.html is not None
    assert "<script>" in normalized.parsed.html  # raw HTML is stored, sanitised on display
    assert "Read more" in normalized.body_text


def test_inline_and_regular_attachments() -> None:
    parsed = parse_message(load_fixture("05-nested-multipart.eml"))
    logo, report = parsed.attachments

    assert logo.is_inline
    assert logo.content_id == "logo-0001@example.com"
    assert logo.data.startswith(b"\x89PNG")
    assert not report.is_inline
    assert report.content_type == "application/pdf"
    assert report.data.startswith(b"%PDF")
    assert "cid:logo-0001@example.com" in (parsed.html or "")


def test_attached_message_is_kept_as_eml() -> None:
    parsed = parse_message(load_fixture("11-forward-rfc822.eml"))
    (attachment,) = parsed.attachments

    assert attachment.content_type == "message/rfc822"
    inner = parse_message(attachment.data)
    assert inner.subject == "Offer for the new printers"
    # The attached mail's text is not mixed into the outer body.
    assert "here is our offer" not in (parsed.text or "")


def test_broken_headers_degrade_gracefully() -> None:
    parsed = parse_message(load_fixture("12-broken-headers-rfc2231.eml"))

    assert parsed.message_id is None
    assert parsed.sent_at is None
    assert parsed.to == ()
    # Path components in attachment names are stripped.
    assert parsed.attachments[1].filename == "passwd"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not a mail at all",
        b"Subject: only headers\r\n",
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\nbroken",
        b"Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n@@@",
        b"Subject: nul\x00byte\r\n\r\nbody with \x00 nul",
    ],
)
def test_garbage_never_raises(raw: bytes) -> None:
    normalized = normalize_message(raw)

    assert "\x00" not in normalized.body_text
    assert "\x00" not in normalized.parsed.subject


@pytest.mark.parametrize(
    ("data", "charset", "expected"),
    [
        ("Grüße".encode(), "utf-8", "Grüße"),
        ("Grüße".encode("latin-1"), "utf-8", "Grüße"),
        ("Grüße".encode("cp1252"), "iso-8859-1", "Grüße"),
        ("„Zitat“".encode("cp1252"), "iso-8859-1", "„Zitat“"),
        ("Grüße".encode(), "x-unknown", "Grüße"),
        ("Grüße".encode(), "us-ascii", "Grüße"),
        ("Grüße".encode(), None, "Grüße"),
        ("Привет".encode("koi8-r"), "koi8-r", "Привет"),
    ],
)
def test_decode_bytes(data: bytes, charset: str | None, expected: str) -> None:
    assert decode_bytes(data, charset) == expected

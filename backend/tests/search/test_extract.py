"""Attachment text extraction in the isolated child process. All documents are synthetic."""

import pytest

from app.core.config import SearchSettings
from app.search import extract
from app.search.extract import Extraction, Kind, detect_kind, extract_text
from tests.search.documents import make_docx, make_pdf

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def settings() -> SearchSettings:
    return SearchSettings(extraction_timeout=20)


@pytest.mark.parametrize(
    ("content_type", "filename", "kind"),
    [
        ("application/pdf", None, Kind.PDF),
        ("Application/PDF; name=x.pdf", "x.pdf", Kind.PDF),
        (DOCX_TYPE, "a.docx", Kind.DOCX),
        ("text/plain; charset=utf-8", None, Kind.TEXT),
        ("text/html", None, Kind.HTML),
        ("application/octet-stream", "Report.PDF", Kind.PDF),
        ("application/octet-stream", "notes.txt", Kind.TEXT),
        ("application/octet-stream", "photo.jpg", Kind.IMAGE),
        ("application/octet-stream", None, None),
        ("image/png", "scan.pdf", Kind.IMAGE),
        ("image/gif", "anim.gif", None),
        ("application/msword", "old.doc", None),
    ],
)
def test_detect_kind(content_type: str, filename: str | None, kind: Kind | None) -> None:
    assert detect_kind(content_type, filename) == kind


async def test_pdf(settings: SearchSettings) -> None:
    data = make_pdf("Invoice 4711 for consulting", "Second page (payment terms)")

    result = await extract_text(data, Kind.PDF, settings)

    assert result.status == "ok"
    assert result.text is not None
    assert "Invoice 4711 for consulting" in result.text
    assert "Second page (payment terms)" in result.text


async def test_docx(settings: SearchSettings) -> None:
    data = make_docx("Meeting notes", "Budget &amp; timeline agreed")

    result = await extract_text(data, Kind.DOCX, settings)

    assert result == Extraction("ok", "Meeting notes\n\nBudget & timeline agreed")


async def test_docx_with_dtd_is_rejected(settings: SearchSettings) -> None:
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        "<w:document><w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>"
    )

    result = await extract_text(make_docx(document_xml=bomb), Kind.DOCX, settings)

    assert result == Extraction("unreadable")


async def test_text_in_legacy_charset(settings: SearchSettings) -> None:
    result = await extract_text("Grüße aus Köln".encode("cp1252"), Kind.TEXT, settings)

    assert result == Extraction("ok", "Grüße aus Köln")


async def test_control_characters_are_removed(settings: SearchSettings) -> None:
    result = await extract_text(b"a\x00b\x07c\td", Kind.TEXT, settings)

    assert result == Extraction("ok", "a b c\td")


async def test_html(settings: SearchSettings) -> None:
    html = b"<html><head><style>p{}</style></head><body><p>Hello</p><p>World</p></body></html>"

    result = await extract_text(html, Kind.HTML, settings)

    assert result.status == "ok"
    assert result.text is not None
    assert "Hello" in result.text and "World" in result.text
    assert "p{}" not in result.text


async def test_text_is_truncated_to_the_limit() -> None:
    settings = SearchSettings(attachment_max_chars=1000)

    result = await extract_text(b"x" * 5000, Kind.TEXT, settings)

    assert result.text is not None
    assert len(result.text) == 1000


async def test_empty_document(settings: SearchSettings) -> None:
    assert await extract_text(b"   ", Kind.TEXT, settings) == Extraction("empty")


async def test_broken_files_are_unreadable(settings: SearchSettings) -> None:
    assert await extract_text(b"%PDF-1.4 garbage", Kind.PDF, settings) == Extraction("unreadable")
    assert await extract_text(b"not a zip", Kind.DOCX, settings) == Extraction("unreadable")


async def test_files_above_the_size_limit_are_skipped() -> None:
    settings = SearchSettings(attachment_max_bytes=1024)

    assert await extract_text(b"x" * 1025, Kind.TEXT, settings) == Extraction("too_large")


async def test_slow_extraction_is_killed() -> None:
    settings = SearchSettings(extraction_timeout=0.001)

    assert await extract_text(make_pdf("slow"), Kind.PDF, settings) == Extraction("timeout")


def test_child_gets_no_secrets_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", "secret")

    env = extract._child_env()

    assert not any(key.startswith("OLLAMAIL") for key in env)
    assert set(env) <= {
        "PATH",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "OMP_THREAD_LIMIT",
    }

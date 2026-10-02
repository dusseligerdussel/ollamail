"""Unit tests: chunking of mail text."""

import itertools
from datetime import UTC, datetime

import pytest

from app.search.chunking import Chunk, chunk_text, clean, heading, split_text, ts_config_for

PARAGRAPH = (
    "The quarterly report is attached. Revenue grew in all regions. "
    "Please review the numbers before Friday. "
)


def test_short_text_is_one_chunk() -> None:
    assert split_text("  Hello\n\n\n\nworld  ", size=200, overlap=20) == ["Hello\n\nworld"]


def test_empty_text_has_no_chunks() -> None:
    assert split_text(" \n\t ", size=200, overlap=20) == []


@pytest.mark.parametrize(("size", "overlap"), [(200, 0), (300, 50), (1200, 200)])
def test_chunks_respect_size_and_cover_all_text(size: int, overlap: int) -> None:
    text = "\n\n".join(PARAGRAPH * (i % 4 + 1) for i in range(20))

    chunks = split_text(text, size=size, overlap=overlap)

    assert len(chunks) > 1
    assert all(0 < len(chunk) <= size for chunk in chunks)
    words = set(clean(text).split())
    assert words == set(" ".join(chunks).split())


def test_neighbouring_chunks_overlap_at_word_boundaries() -> None:
    text = " ".join(f"word{i}" for i in range(400))

    chunks = split_text(text, size=200, overlap=40)

    for previous, current in itertools.pairwise(chunks):
        first_word = current.split()[0]
        assert first_word in previous.split()
        # The shared part is about the overlap, not more than a chunk.
        shared = previous[previous.index(first_word) :]
        assert 0 < len(shared) <= 40
        assert current.startswith(shared)


def test_paragraphs_that_fit_are_not_cut() -> None:
    first = "First paragraph about the budget. " * 4
    second = "Second paragraph about the timeline. " * 4

    chunks = split_text(f"{first}\n\n{second}", size=len(second) + 20, overlap=0)

    assert chunks == [first.strip(), second.strip()]


def test_long_sentences_are_split_at_sentence_ends() -> None:
    text = " ".join(f"Sentence number {i} ends here." for i in range(60))

    chunks = split_text(text, size=150, overlap=0)

    assert all(chunk.endswith(".") for chunk in chunks)


def test_text_without_spaces_is_cut_hard() -> None:
    chunks = split_text("x" * 1000, size=300, overlap=0)

    assert [len(c) for c in chunks] == [299, 299, 299, 103]


def test_overlap_must_be_smaller_than_size() -> None:
    with pytest.raises(ValueError):
        split_text("a b c " * 100, size=100, overlap=100)


def test_heading_contains_sender_date_subject_and_attachment() -> None:
    result = heading(
        sender={"name": "Erika Example", "address": "erika@example.org"},
        date=datetime(2026, 9, 30, 23, 0, tzinfo=UTC),
        subject="  Quarterly\n report ",
        attachment="report.pdf",
    )

    assert result == (
        "From: Erika Example <erika@example.org>\n"
        "Date: 2026-09-30\n"
        "Subject: Quarterly report\n"
        "Attachment: report.pdf"
    )


def test_heading_skips_missing_fields() -> None:
    assert heading(sender={"address": "a@example.org"}, date=None, subject="") == (
        "From: a@example.org"
    )
    assert heading(sender=None, date=None, subject="") == ""


def test_chunks_carry_the_heading_in_the_embedded_text() -> None:
    chunks = chunk_text(PARAGRAPH * 20, context="Subject: Report", size=300, overlap=50)

    assert len(chunks) > 1
    assert all(chunk.heading == "Subject: Report" for chunk in chunks)
    assert chunks[0].text.startswith("Subject: Report\n\nThe quarterly report")
    assert Chunk("Subject: Only", "").text == "Subject: Only"


@pytest.mark.parametrize(
    ("language", "config"),
    [("de", "german"), ("EN", "english"), ("fr", "simple"), (None, "simple")],
)
def test_text_search_configuration_per_language(language: str | None, config: str) -> None:
    assert ts_config_for(language) == config

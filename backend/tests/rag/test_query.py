"""Unit tests: filters from the question, combined with UI filters; reranker switch."""

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.config import LLMSettings, RagSettings, Settings
from app.rag.query import Choice, QueryAnalysis, QueryContext, combine, search_query
from app.rag.rerank import reranker_enabled
from app.rag.schemas import RagFilters

WORK, PRIVATE = uuid.uuid4(), uuid.uuid4()
NEWSLETTER = uuid.uuid4()
BERLIN = ZoneInfo("Europe/Berlin")


def context() -> QueryContext:
    return QueryContext(
        mailboxes=[Choice("m1", WORK, "Work"), Choice("m2", PRIVATE, "Private")],
        categories=[Choice("newsletter", NEWSLETTER, "Newsletter")],
        timezone=BERLIN,
    )


def test_extracted_filters_are_mapped_and_marked() -> None:
    analysis = QueryAnalysis(
        search_query="  invoice   hosting ",
        since=date(2026, 9, 1),
        until=date(2026, 9, 30),
        sender=" Max  Example ",
        mailbox="M2",
        category="newsletter",
    )

    applied = combine(RagFilters(), analysis, context())

    assert applied.mailbox_ids == [PRIVATE]
    assert applied.category_ids == [NEWSLETTER]
    assert applied.sender == "Max Example"
    # Days in the user's time zone; ``until`` becomes exclusive.
    assert applied.since == datetime(2026, 9, 1, tzinfo=BERLIN).astimezone(UTC)
    assert applied.until == datetime(2026, 10, 1, tzinfo=BERLIN).astimezone(UTC)
    assert applied.extracted == ["mailbox_ids", "category_ids", "sender", "since", "until"]
    assert search_query("original", analysis) == "invoice hosting"


def test_ui_filters_win() -> None:
    ui = RagFilters(mailbox_ids=[WORK], sender="erika", since=datetime(2026, 1, 1, tzinfo=UTC))
    analysis = QueryAnalysis(mailbox="m2", sender="max", until=date(2026, 3, 1))

    applied = combine(ui, analysis, context())

    assert applied.mailbox_ids == [WORK]
    assert applied.sender == "erika"
    # A period from the UI is not mixed with one from the question.
    assert (applied.since, applied.until) == (ui.since, None)
    assert applied.extracted == []


def test_unknown_keys_and_invalid_periods_are_ignored() -> None:
    analysis = QueryAnalysis(
        mailbox="m9", category="secret", since=date(2026, 5, 2), until=date(2026, 5, 1)
    )

    applied = combine(RagFilters(), analysis, context())

    assert applied.model_dump(exclude={"extracted"}) == RagFilters().model_dump()
    assert applied.extracted == []


def test_without_analysis_the_question_is_searched() -> None:
    assert combine(RagFilters(sender="x"), None, context()).sender == "x"
    assert search_query("When is my flight?", None) == "When is my flight?"
    assert search_query("When is my flight?", QueryAnalysis(search_query=" ")) == (
        "When is my flight?"
    )


@pytest.mark.parametrize(
    ("profile", "override", "expected"),
    [
        ("cpu", None, False),
        ("gpu-consumer", None, True),
        ("gpu-server", None, True),
        ("cpu", True, True),
        ("gpu-server", False, False),
    ],
)
def test_reranker_follows_hardware_profile(
    profile: str, override: bool | None, expected: bool
) -> None:
    settings = Settings(
        llm=LLMSettings.model_validate({"profile": profile}),
        rag=RagSettings(reranker_enabled=override),
    )
    assert reranker_enabled(settings) is expected

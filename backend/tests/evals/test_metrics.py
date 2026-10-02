"""Metrics of the evaluation (pure functions)."""

from datetime import date

import pytest

from app.ai.llm.metrics import LLMCallMetrics
from app.evals.metrics import (
    Confusion,
    ExpectedTask,
    PredictedTodo,
    answer_contains,
    call_stats,
    mean_reciprocal_rank,
    percentile,
    recall_at_k,
    score_todos,
    source_rank,
    title_similarity,
)

FRIDAY = date(2026, 10, 9)


def test_confusion_accuracy_matrix_and_recall() -> None:
    confusion = Confusion()
    for expected, predicted in [("info", "info"), ("info", "spam"), ("spam", "spam")]:
        confusion.add(expected, predicted)
    confusion.add("spam", "error")

    assert confusion.total == 4
    assert confusion.accuracy == 0.5
    assert confusion.labels(("spam", "info")) == ["spam", "info", "error"]
    assert confusion.matrix(("info", "spam")) == {
        "info": {"info": 1, "spam": 1, "error": 0},
        "spam": {"info": 0, "spam": 1, "error": 1},
        "error": {"info": 0, "spam": 0, "error": 0},
    }
    assert confusion.per_class_recall(("info", "spam")) == {"info": 0.5, "spam": 0.5}
    assert Confusion().accuracy == 0.0


@pytest.mark.parametrize(
    ("a", "b", "similar"),
    [
        ("Angebot für Kunde Nord prüfen", "Angebot Kunde Nord prüfen", True),
        ("Prüfen: Angebot Kunde Nord", "Angebot Kunde Nord prüfen", True),
        ("Review the contract draft", "review contract draft", True),
        ("Rechnung bezahlen", "Angebot Kunde Nord prüfen", False),
        ("", "Anything", False),
    ],
)
def test_title_similarity(a: str, b: str, similar: bool) -> None:
    assert (title_similarity(a, b) >= 0.6) is similar


def test_todos_match_by_keyword_or_title_and_check_dates_exactly() -> None:
    expected = [
        ExpectedTask("Angebot für Kunde Nord prüfen", ("angebot",), FRIDAY),
        ExpectedTask("Reisekosten einreichen", (), None),
        ExpectedTask("Vertrag unterschreiben", ("vertrag",), None),
    ]
    predicted = [
        # Keyword in the description, wrong date.
        PredictedTodo("Kunde Nord", "Das Angebot durchsehen", date(2026, 10, 8)),
        # Fuzzy title match, both without date.
        PredictedTodo("Die Reisekosten einreichen", None, None),
        PredictedTodo("Kaffee kaufen", None, None),
    ]

    score = score_todos(expected, predicted)

    assert (score.true_positives, score.false_positives, score.false_negatives) == (2, 1, 1)
    assert (score.due_checked, score.due_correct) == (2, 1)
    assert score.precision == pytest.approx(2 / 3)
    assert score.recall == pytest.approx(2 / 3)
    assert score.f1 == pytest.approx(2 / 3)
    assert score.due_accuracy == 0.5


def test_one_prediction_matches_one_expected_todo_only() -> None:
    expected = [ExpectedTask("Bericht schicken", ("bericht",), None)] * 2
    score = score_todos(expected, [PredictedTodo("Bericht schicken", None, None)])
    assert (score.true_positives, score.false_negatives) == (1, 1)


def test_empty_todo_lists_are_perfect() -> None:
    score = score_todos([], [])
    assert (score.precision, score.recall, score.due_accuracy) == (1.0, 1.0, 1.0)


def test_source_rank_counts_each_mail_once() -> None:
    sources = ["m1", "m1", "m2", "m3"]
    assert source_rank(sources, ["m3"]) == 3
    assert source_rank(sources, ["m2", "m3"]) == 2
    assert source_rank(sources, ["m9"]) is None
    ranks = [1, 3, None, 2]
    assert recall_at_k(ranks, 1) == 0.25
    assert recall_at_k(ranks, 3) == 0.75
    assert mean_reciprocal_rank(ranks) == pytest.approx((1 + 1 / 3 + 1 / 2) / 4)
    assert recall_at_k([], 3) == 0.0


def test_answer_needs_every_keyword_group() -> None:
    groups = [["14:30", "14.30"], ["raum b2"]]
    assert answer_contains("Das Kick-off ist um 14.30 Uhr in Raum B2 [1].", groups)
    assert not answer_contains("Das Kick-off ist um 14:30 Uhr [1].", groups)
    assert answer_contains("Zahlung an Müller", [["muller"]])


def _call(
    task: str, ms: int, prompt: int | None, completion: int | None, ok: bool = True
) -> LLMCallMetrics:
    return LLMCallMetrics(
        task=task,
        operation="structured",
        endpoint="default",
        provider="ollama",
        model="m",
        prompt_version=None,
        duration_ms=ms,
        success=ok,
        prompt_tokens=prompt,
        completion_tokens=completion,
    )


def test_call_stats() -> None:
    stats = call_stats(
        [
            _call("triage", 1000, 300, 10),
            _call("triage", 3000, 500, 30),
            _call("triage", 2000, None, None, ok=False),
        ]
    )

    assert stats.calls == 3
    assert stats.failed == 1
    assert stats.seconds_mean == 2.0
    assert stats.seconds_p95 == 3.0
    assert stats.tokens_per_second == 10.0
    assert stats.processed_tokens_per_second == 210.0
    assert not stats.estimated
    assert call_stats([]).tokens_per_second is None


def test_percentile() -> None:
    assert percentile([], 0.5) == 0.0
    assert percentile([3.0, 1.0, 2.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0

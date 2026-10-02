"""Eval data set and scoring (no database; the model is a fake). The real run against
Ollama: ``python -m app.todos.evaluation --model <name>``."""

import json
import re

import pytest

from app.core.config import TodosSettings
from app.todos.dates import parse_due_phrase
from app.todos.evaluation import EvalCase, case_input, evaluate, load_cases, main
from tests.todos.conftest import FakeLLM

CASES = load_cases()

# Example domains only (RFC 2606): the data set must never contain real addresses.
_ADDRESS = re.compile(r"[\w.+-]+@([\w-]+\.)*([\w-]+\.[a-z]+)")


def test_data_set_covers_both_languages_and_all_outcomes() -> None:
    assert len(CASES) >= 10
    assert len({case.id for case in CASES}) == len(CASES)
    assert {case.language for case in CASES} == {"de", "en"}
    assert any(not (c.expected.todos or c.expected.updates or c.expected.done) for c in CASES)
    assert any(c.expected.updates for c in CASES)
    assert any(c.expected.done for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_due_dates_follow_from_the_mail(case: EvalCase) -> None:
    reference = case_input(case).reference
    for expected in [*case.expected.todos, *case.expected.updates]:
        if expected.due_phrase is None:
            continue
        assert expected.due_phrase in case.body
        assert parse_due_phrase(expected.due_phrase, reference) == expected.due_date


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_only_example_addresses(case: EvalCase) -> None:
    text = case.model_dump_json()
    for match in _ADDRESS.finditer(text):
        assert match.group(2) in {"example.org", "example.com"}, match.group(0)


def _perfect_answer(case: EvalCase) -> str:
    todos = [
        {
            "title": expected.keywords[0],
            "due_phrase": expected.due_phrase,
            # A wrong guess: the phrase must win.
            "due_date": "2026-12-24" if expected.due_phrase else None,
            "confidence": 0.9,
        }
        for expected in case.expected.todos
    ]
    todos += [
        {
            "title": "update",
            "due_phrase": update.due_phrase,
            "updates": update.todo,
            "confidence": 1,
        }
        for update in case.expected.updates
    ]
    return json.dumps({"todos": todos, "done": case.expected.done})


async def test_perfect_answers_score_100_percent(fake_llm: FakeLLM) -> None:
    fake_llm.provider.answers.extend(_perfect_answer(case) for case in CASES)

    report = await evaluate(fake_llm.gateway, CASES, "fake", TodosSettings())

    assert report.passed == len(CASES), report.render()
    assert (report.precision, report.recall, report.due_accuracy) == (1.0, 1.0, 1.0)


async def test_wrong_answers_are_counted(fake_llm: FakeLLM) -> None:
    case = next(c for c in CASES if c.id == "de-report-next-friday")
    newsletter = next(c for c in CASES if c.id == "de-newsletter")
    fake_llm.answer([{"title": "Quartalsbericht", "due_date": "2026-10-16", "confidence": 1}])
    fake_llm.answer([{"title": "Jacke kaufen", "confidence": 0.9}])

    report = await evaluate(fake_llm.gateway, [case, newsletter], "fake", TodosSettings())

    assert report.passed == 0
    assert (report.precision, report.recall, report.due_accuracy) == (0.5, 1.0, 0.0)
    assert "de-newsletter" in report.render()


def test_cli_rejects_unknown_options() -> None:
    with pytest.raises(SystemExit):
        main(["--unknown"])

"""The evaluation data set: size, coverage, consistency and invented data only."""

import re
from collections import Counter
from datetime import date

import pytest

from app.evals.dataset import CATEGORIES, OWNER_TIMEZONE, Dataset, EvalMail, load_dataset
from app.evals.todos import transient_mail
from app.todos.dates import parse_due_phrase
from app.todos.extraction import reference_date

DATASET = load_dataset()
# Example domains only (RFC 2606): the data set must never contain real addresses.
_ADDRESS = re.compile(r"[\w.+-]+@([\w-]+\.)*([\w-]+\.[a-z]+)")
_URL = re.compile(r"https?://([\w.-]+)")


def test_size_and_coverage() -> None:
    mails = DATASET.mails
    assert 150 <= len(mails) <= 300
    assert len({m.id for m in mails}) == len(mails)
    for language in ("de", "en"):
        categories = Counter(m.category for m in mails if m.language == language)
        assert set(categories) == set(CATEGORIES), language
        assert min(categories.values()) >= 5, language
    assert sum(len(m.todos) for m in mails) >= 50
    assert any(t.due_date is None for m in mails for t in m.todos)


def test_subjects_are_unique() -> None:
    # The fake model of the tests finds a mail by its subject.
    subjects = Counter(m.subject for m in DATASET.mails)
    assert [s for s, n in subjects.items() if n > 1] == []


def test_questions_refer_to_mails_of_their_language() -> None:
    questions = DATASET.questions
    by_id = {m.id: m for m in DATASET.mails}
    assert len(questions) >= 40
    assert len({q.id for q in questions}) == len(questions)
    assert {q.language for q in questions} == {"de", "en"}
    assert sum(q.no_answer for q in questions) >= 6
    for question in questions:
        for source in question.sources:
            assert source in by_id, question.id
            assert by_id[source].language == question.language, question.id


@pytest.mark.parametrize("mail", DATASET.mails, ids=lambda m: m.id)
def test_expected_due_dates_follow_from_the_mail(mail: EvalMail) -> None:
    reference = reference_date(transient_mail(mail)[0], OWNER_TIMEZONE)
    assert reference == mail.sent_at.date()
    for todo in mail.todos:
        assert todo.keywords, mail.id
        assert all(k == k.lower() for k in todo.keywords), mail.id
        if todo.due_phrase is None:
            assert todo.due_date is None, mail.id
            continue
        assert todo.due_phrase in mail.body, mail.id
        assert parse_due_phrase(todo.due_phrase, reference) == todo.due_date, mail.id


def test_answer_keywords_occur_in_the_source_mails() -> None:
    by_id = {m.id: m for m in DATASET.mails}
    for question in DATASET.questions:
        text = " ".join(f"{by_id[s].subject} {by_id[s].body}" for s in question.sources).lower()
        for group in question.answer:
            assert any(option.lower() in text for option in group), (question.id, group)


def test_only_invented_addresses_and_urls() -> None:
    text = DATASET.model_dump_json(by_alias=True)
    for match in _ADDRESS.finditer(text):
        assert match.group(2) in {"example.org", "example.com"}, match.group(0)
    for match in _URL.finditer(text):
        host = match.group(1).rstrip(".")
        assert host.endswith(("example.org", "example.com")), match.group(0)


def test_mails_are_in_one_week() -> None:
    days = {m.sent_at.date() for m in DATASET.mails}
    assert min(days) >= date(2026, 10, 5)
    assert max(days) <= date(2026, 10, 11)


def test_subset_keeps_the_mix_and_drops_orphaned_questions() -> None:
    sample = DATASET.subset(limit=40)
    assert len(sample.mails) == 40
    assert len({m.category for m in sample.mails}) >= 5
    kept = {m.id for m in sample.mails}
    assert all(set(q.sources) <= kept for q in sample.questions)
    assert any(q.no_answer for q in sample.questions)

    german = DATASET.subset(languages={"de"})
    assert {m.language for m in german.mails} == {"de"}
    assert {q.language for q in german.questions} == {"de"}
    assert DATASET.subset() == Dataset(mails=DATASET.mails, questions=DATASET.questions)

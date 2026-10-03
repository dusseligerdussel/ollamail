"""The evaluation data set: size, coverage, consistency and invented data only."""

import re
from collections import Counter
from datetime import date

import pytest

from app.evals.dataset import CATEGORIES, OWNER_TIMEZONE, Dataset, EvalMail, load_dataset
from app.evals.injection import contains_canary
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
    subjects = Counter(m.subject for m in [*DATASET.mails, *DATASET.injections])
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


def test_injection_cases() -> None:
    # A fixed category of the evaluation (#170): both languages, several kinds of
    # demands, and cases whose instructions sit in otherwise legitimate mails.
    mails = DATASET.injection_mails
    assert len(mails) >= 24
    assert all(m.injection is not None for m in DATASET.injections)
    assert {m.language for m in mails} == {"de", "en"}
    assert sum(m.injection is not None and m.injection.demands is not None for m in mails) >= 16
    assert sum(m.injection is not None and bool(m.injection.canary) for m in mails) >= 12
    assert sum(m.category != "spam" for m in mails) >= 8
    assert not {m.id for m in DATASET.mails} & {m.id for m in DATASET.injections}


@pytest.mark.parametrize("mail", DATASET.injection_mails, ids=lambda m: m.id)
def test_injection_passage_and_canary(mail: EvalMail) -> None:
    injection = mail.injection
    assert injection is not None
    assert injection.passage in mail.body
    rest = f"{mail.subject} {mail.body.replace(injection.passage, ' ')}"
    for word in injection.canary:
        assert word == word.lower()
        # Only following the instructions can bring a canary word into an output.
        assert contains_canary(injection.passage, [word]), word
        assert not contains_canary(rest, [word]), word


@pytest.mark.parametrize("mail", [*DATASET.mails, *DATASET.injections], ids=lambda m: m.id)
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
    days = {m.sent_at.date() for m in [*DATASET.mails, *DATASET.injections]}
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
    assert DATASET.subset() == Dataset(
        mails=DATASET.mails, questions=DATASET.questions, injections=DATASET.injections
    )
    assert {m.language for m in german.injections} == {"de"}
    assert DATASET.subset(limit=10).injections == DATASET.injections


def test_question_sample_keeps_all_mails_and_the_mix() -> None:
    sample = DATASET.sample_questions(25)
    assert len(sample.questions) == 25
    assert sample.mails == DATASET.mails
    assert {q.language for q in sample.questions} == {"de", "en"}
    assert any(q.no_answer for q in sample.questions)
    assert any(not q.no_answer for q in sample.questions)
    assert DATASET.sample_questions(0) == DATASET

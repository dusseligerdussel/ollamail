"""Selection of few-shot examples: recency, embeddings, fallbacks (PostgreSQL)."""

from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMTask, LLMUnavailableError
from app.core.config import TriageSettings
from app.triage.categories import effective_categories
from app.triage.classify import MailView
from app.triage.feedback import cosine, select_examples
from app.triage.models import TriageCategoryPreference, TriageFeedback
from app.triage.service import correct
from tests.triage.conftest import Account

pytestmark = pytest.mark.db

TOPICS = ("invoice", "football", "garden")


class KeywordEmbeddings:
    """Embeds texts as keyword counts over ``TOPICS``."""

    def __init__(self, model: str = "embed-a", fail: bool = False) -> None:
        self.model = model
        self.fail = fail
        self.batches: list[int] = []

    async def embed(
        self, texts: Sequence[str], *, task: LLMTask = LLMTask.EMBEDDINGS
    ) -> list[list[float]]:
        if self.fail:
            raise LLMUnavailableError("no embedding model")
        self.batches.append(len(texts))
        return [[float(t.lower().count(topic)) + 0.01 for topic in TOPICS] for t in texts]

    async def assigned_model(self, task: LLMTask) -> str:
        return self.model


QUERY = MailView("Your invoice", None, "billing@example.org", "to", None, "invoice attached")


async def _examples(account: Account) -> None:
    categories = await effective_categories(account.session, account.user.id)
    by_key = {c.key: c.id for c in categories}
    for subject, key in [
        ("Invoice 2026-01", "action_required"),
        ("Football results", "newsletter"),
        ("Garden tips", "newsletter"),
        ("Football tickets", "info"),
    ]:
        message = await account.message(subject, body=subject.lower())
        await correct(account.session, account.user.id, message.id, by_key[key], 2)


def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0)
    assert cosine([1, 0], [1, 0, 0]) == -1
    assert cosine([0, 0], [1, 0]) == -1


async def test_without_embeddings_the_most_recent_corrections_are_used(
    db_session: AsyncSession, account: Account
) -> None:
    await _examples(account)
    categories = await effective_categories(db_session, account.user.id)
    settings = TriageSettings(few_shot_examples=2, few_shot_embeddings=False)

    examples = await select_examples(db_session, account.user.id, QUERY, categories, settings)

    assert [e.mail.subject for e in examples] == ["Football tickets", "Garden tips"]


async def test_embeddings_pick_the_most_similar_and_are_cached(
    db_session: AsyncSession, account: Account
) -> None:
    await _examples(account)
    categories = await effective_categories(db_session, account.user.id)
    settings = TriageSettings(few_shot_examples=1)
    llm = KeywordEmbeddings()

    examples = await select_examples(
        db_session, account.user.id, QUERY, categories, settings, llm=llm
    )
    again = await select_examples(db_session, account.user.id, QUERY, categories, settings, llm=llm)

    assert [(e.mail.subject, e.category_key) for e in examples] == [
        ("Invoice 2026-01", "action_required")
    ]
    assert again == examples
    # Four examples embedded once, then only the query.
    assert llm.batches == [4, 1, 1]
    stored = (await db_session.scalars(select(TriageFeedback))).all()
    assert {row.embedding_model for row in stored} == {"embed-a"}

    await select_examples(
        db_session, account.user.id, QUERY, categories, settings, llm=KeywordEmbeddings("embed-b")
    )
    # A different embedding model re-embeds the examples.
    assert {row.embedding_model for row in stored} == {"embed-b"}


async def test_embedding_failure_falls_back_to_recent(
    db_session: AsyncSession, account: Account
) -> None:
    await _examples(account)
    categories = await effective_categories(db_session, account.user.id)
    settings = TriageSettings(few_shot_examples=1)

    examples = await select_examples(
        db_session, account.user.id, QUERY, categories, settings, llm=KeywordEmbeddings(fail=True)
    )

    assert [e.mail.subject for e in examples] == ["Football tickets"]


async def test_no_embeddings_needed_when_all_examples_fit(
    db_session: AsyncSession, account: Account
) -> None:
    await _examples(account)
    categories = await effective_categories(db_session, account.user.id)
    llm = KeywordEmbeddings()

    examples = await select_examples(
        db_session, account.user.id, QUERY, categories, TriageSettings(few_shot_examples=4), llm=llm
    )

    assert len(examples) == 4
    assert llm.batches == []


async def test_examples_of_hidden_categories_are_skipped(
    db_session: AsyncSession, account: Account
) -> None:
    await _examples(account)
    everything = await effective_categories(db_session, account.user.id)
    newsletter = next(c.id for c in everything if c.key == "newsletter")
    db_session.add(
        TriageCategoryPreference(user_id=account.user.id, category_id=newsletter, hidden=True)
    )
    await db_session.flush()
    categories = await effective_categories(db_session, account.user.id)
    settings = TriageSettings(few_shot_examples=5, few_shot_embeddings=False)

    examples = await select_examples(db_session, account.user.id, QUERY, categories, settings)

    assert sorted(e.mail.subject for e in examples) == ["Football tickets", "Invoice 2026-01"]


async def test_zero_examples_configured(db_session: AsyncSession, account: Account) -> None:
    await _examples(account)
    categories = await effective_categories(db_session, account.user.id)

    assert (
        await select_examples(
            db_session, account.user.id, QUERY, categories, TriageSettings(few_shot_examples=0)
        )
        == []
    )

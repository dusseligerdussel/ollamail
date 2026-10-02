"""Few-shot examples never cross users (docs/PRIVACY.md, Zweckbindung & Trennung)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import TriageSettings
from app.triage.categories import effective_categories
from app.triage.feedback import suggest_sender_rules
from app.triage.models import TriageCategory, TriageFeedback
from app.triage.service import correct, triage_message
from tests.triage.conftest import Account, FakeLLM, answer

pytestmark = pytest.mark.db


async def _correct(account: Account, subject: str, key: str, sender: str) -> None:
    session = account.session
    message = await account.message(subject, sender=sender, body=f"Body of {subject}")
    categories = await effective_categories(session, account.user.id)
    category_id = next(c.id for c in categories if c.key == key)
    await correct(session, account.user.id, message.id, category_id, 3)


@pytest.mark.parametrize("embeddings", [False, True])
async def test_examples_come_only_from_the_owner(
    db_session: AsyncSession,
    account: Account,
    other_account: Account,
    fake_llm: FakeLLM,
    embeddings: bool,
) -> None:
    for i in range(3):
        await _correct(account, f"Own example {i}", "newsletter", "own@example.org")
        await _correct(other_account, f"Foreign example {i}", "spam", "foreign@example.org")
    message = await account.message("Fresh mail")
    fake_llm.answer(answer("info"))
    settings = TriageSettings(few_shot_examples=2, few_shot_embeddings=embeddings)

    await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=settings)

    (prompt,) = fake_llm.prompts()
    assert prompt.count("Subject: Own example") == 2
    assert "Foreign" not in prompt
    assert "foreign@example.org" not in prompt


async def test_examples_of_the_same_user_in_another_users_mailbox_are_ignored(
    db_session: AsyncSession, account: Account, other_account: Account, fake_llm: FakeLLM
) -> None:
    """Defence in depth: a feedback row pointing at a message the user does not own
    (cannot be created via the API) is not used either."""
    foreign_message = await other_account.message("Not yours")
    categories = await effective_categories(db_session, account.user.id)
    db_session.add(
        TriageFeedback(
            user_id=account.user.id,
            message_id=foreign_message.id,
            category_id=categories[0].id,
            priority=1,
        )
    )
    message = await account.message("Fresh mail")
    fake_llm.answer(answer("info"))

    await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=TriageSettings())

    assert "Not yours" not in fake_llm.prompts()[0]


async def test_own_categories_of_others_are_not_offered(
    db_session: AsyncSession, account: Account, other_account: Account, fake_llm: FakeLLM
) -> None:
    db_session.add(
        TriageCategory(
            owner_user_id=other_account.user.id,
            name="Secret project",
            description="Mails about the secret project",
        )
    )
    message = await account.message()
    fake_llm.answer(answer("info"))

    await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=TriageSettings())

    assert "secret" not in fake_llm.prompts()[0].lower()


async def test_rule_suggestions_are_per_user(
    db_session: AsyncSession, account: Account, other_account: Account
) -> None:
    for i in range(3):
        await _correct(account, f"Digest {i}", "newsletter", "News@Example.org")
        await _correct(other_account, f"Offer {i}", "spam", "shop@example.org")
    # Conflicting corrections: no suggestion.
    await _correct(account, "Mixed 1", "info", "mixed@example.org")
    for i in range(2):
        await _correct(account, f"Mixed {i + 2}", "spam", "mixed@example.org")

    suggestions = await suggest_sender_rules(db_session, account.user.id, min_corrections=3)

    assert [(s.sender, s.corrections, s.priority) for s in suggestions] == [
        ("news@example.org", 3, 3)
    ]
    assert await suggest_sender_rules(db_session, uuid.uuid4(), min_corrections=1) == []

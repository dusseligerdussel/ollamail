"""Few-shot examples never cross users (docs/PRIVACY.md, Zweckbindung & Trennung)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import TriageSettings
from app.mail.models import Folder, Mailbox, MailboxType
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


async def test_shared_mailboxes_learn_only_from_their_own_corrections(
    db_session: AsyncSession, account: Account, other_account: Account, fake_llm: FakeLLM
) -> None:
    """Corrections in a shared mailbox (#34) apply mailbox-wide: they are examples for
    that mailbox, whoever made them, and never for personal mailboxes; personal
    corrections never reach the shared mailbox."""
    team_box = Mailbox(
        type=MailboxType.IMAP, display_name="Team", address="team@example.org", is_shared=True
    )
    db_session.add(team_box)
    await db_session.flush()
    team_inbox = Folder(mailbox_id=team_box.id, remote_id="INBOX", name="Inbox")
    db_session.add(team_inbox)
    await db_session.flush()
    team = Account(db_session, other_account.user, team_box, team_inbox)
    organisation = await effective_categories(db_session, None)
    spam = next(c.id for c in organisation if c.key == "spam")
    for i in range(2):
        shared_message = await team.message(f"Team example {i}", sender="team@example.net")
        await correct(db_session, other_account.user.id, shared_message.id, spam, 3)
        await _correct(account, f"Private example {i}", "newsletter", "own@example.org")

    fresh_team_mail = await team.message("Fresh team mail")
    fake_llm.answer(answer("info"))
    await triage_message(
        db_session, fresh_team_mail.id, llm=fake_llm.gateway, settings=TriageSettings()
    )
    fresh_own_mail = await account.message("Fresh own mail")
    fake_llm.answer(answer("info"))
    await triage_message(
        db_session, fresh_own_mail.id, llm=fake_llm.gateway, settings=TriageSettings()
    )

    team_prompt, own_prompt = fake_llm.prompts()
    assert "Subject: Team example" in team_prompt
    assert "Private example" not in team_prompt
    assert "Subject: Private example" in own_prompt
    assert "Team example" not in own_prompt

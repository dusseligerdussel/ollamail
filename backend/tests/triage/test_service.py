"""Triage of one message: pre-filter, LLM, corrections, categories (PostgreSQL)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import CloudLLMDisabledError
from app.core.config import TriageSettings
from app.processing.steps import StepError
from app.triage.categories import DEFAULT_CATEGORIES, effective_categories
from app.triage.models import (
    TriageCategory,
    TriageCategoryPreference,
    TriageFeedback,
    TriageResult,
    TriageSenderRule,
    TriageSource,
)
from app.triage.service import correct, triage_message
from tests.triage.conftest import Account, FakeLLM, answer, make_account

pytestmark = pytest.mark.db


async def _category_id(session: AsyncSession, account: Account, key: str) -> object:
    categories = await effective_categories(session, account.user.id, include_hidden=True)
    return next(c.id for c in categories if c.key == key)


async def test_migration_creates_the_default_categories(db_session: AsyncSession) -> None:
    categories = await effective_categories(db_session, None)

    assert [(c.builtin_key, c.name, c.description) for c in categories] == [
        (d.key, d.name, d.description) for d in DEFAULT_CATEGORIES
    ]
    assert [c.key for c in categories] == [d.key for d in DEFAULT_CATEGORIES]


async def test_effective_categories_apply_preferences(
    db_session: AsyncSession, account: Account, other_account: Account
) -> None:
    own = TriageCategory(owner_user_id=account.user.id, name="Important", description="Mine")
    foreign = TriageCategory(owner_user_id=other_account.user.id, name="Secret project")
    db_session.add_all([own, foreign])
    await db_session.flush()
    spam = await _category_id(db_session, account, "spam")
    db_session.add_all(
        [
            TriageCategoryPreference(user_id=account.user.id, category_id=spam, hidden=True),
            TriageCategoryPreference(user_id=account.user.id, category_id=own.id, position=0),
        ]
    )
    await db_session.flush()

    visible = await effective_categories(db_session, account.user.id)
    everything = await effective_categories(db_session, account.user.id, include_hidden=True)

    # Own category first (reordered); its key does not clash with the built-in one.
    assert visible[0].id == own.id and visible[0].key == "important_2"
    assert "spam" not in [c.key for c in visible]
    assert foreign.id not in [c.id for c in everything]
    assert [c.hidden for c in everything if c.key == "spam"] == [True]


async def test_header_rule_decides_without_the_model(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    message = await account.message(headers=[("List-Unsubscribe", "<https://example.org/u>")])

    result = await triage_message(
        db_session, message.id, llm=fake_llm.gateway, settings=triage_settings
    )

    assert result is not None
    assert result.category_id == await _category_id(db_session, account, "newsletter")
    assert (result.source, result.rule, result.priority) == (
        TriageSource.RULE,
        "list_unsubscribe",
        3,
    )
    assert result.reason is None and result.model is None
    assert result.write_back_pending
    assert fake_llm.provider.calls == []


async def test_prefilter_can_be_disabled(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM
) -> None:
    message = await account.message(headers=[("Precedence", "bulk")])
    fake_llm.answer(answer("info", 2))

    result = await triage_message(
        db_session,
        message.id,
        llm=fake_llm.gateway,
        settings=TriageSettings(prefilter_enabled=False),
    )

    assert result is not None and result.source == TriageSource.LLM


async def test_sender_rule_decides_without_the_model(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    important = await _category_id(db_session, account, "important")
    db_session.add(
        TriageSenderRule(
            user_id=account.user.id, sender="@example.org", category_id=important, priority=1
        )
    )
    message = await account.message(sender="boss@example.org")

    result = await triage_message(
        db_session, message.id, llm=fake_llm.gateway, settings=triage_settings
    )

    assert result is not None
    assert (result.category_id, result.priority, result.source) == (
        important,
        1,
        TriageSource.SENDER_RULE,
    )


async def test_model_decides_and_result_is_replaced_on_rerun(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    message = await account.message()
    fake_llm.answer(answer("action_required", 1, "Asks for a meeting."), answer("info", 2))

    first = await triage_message(
        db_session, message.id, llm=fake_llm.gateway, settings=triage_settings
    )
    assert first is not None
    assert first.category_id == await _category_id(db_session, account, "action_required")
    assert (first.priority, first.reason, first.source) == (1, "Asks for a meeting.", "llm")
    assert first.prompt_version == "triage@1"
    assert first.model == "qwen2.5:3b"
    first.write_back_pending = False

    second = await triage_message(
        db_session, message.id, llm=fake_llm.gateway, settings=triage_settings
    )

    assert second is not None and second.id == first.id
    assert second.category_id == await _category_id(db_session, account, "info")
    # The category changed, so it has to be written back again.
    assert second.write_back_pending
    rows = await db_session.scalars(
        select(TriageResult).where(TriageResult.message_id == message.id)
    )
    assert len(rows.all()) == 1


async def test_reason_language_follows_the_owner(
    db_session: AsyncSession, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    account = await make_account(db_session, language="de")
    message = await account.message()
    fake_llm.answer(answer("info"))

    await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=triage_settings)

    assert "auf Deutsch" in fake_llm.prompts()[0]


async def test_correction_is_kept_on_reprocessing(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    message = await account.message()
    newsletter = await _category_id(db_session, account, "newsletter")
    await correct(db_session, account.user.id, message.id, newsletter, 3)  # type: ignore[arg-type]

    result = await triage_message(
        db_session, message.id, llm=fake_llm.gateway, settings=triage_settings
    )

    assert result is not None
    assert (result.category_id, result.source) == (newsletter, TriageSource.USER)
    assert fake_llm.provider.calls == []
    feedback = await db_session.scalar(
        select(TriageFeedback).where(TriageFeedback.message_id == message.id)
    )
    assert feedback is not None
    assert (feedback.user_id, feedback.category_id, feedback.priority) == (
        account.user.id,
        newsletter,
        3,
    )


async def test_correcting_twice_replaces_the_example(
    db_session: AsyncSession, account: Account
) -> None:
    message = await account.message()
    info = await _category_id(db_session, account, "info")
    spam = await _category_id(db_session, account, "spam")

    await correct(db_session, account.user.id, message.id, info, 2)  # type: ignore[arg-type]
    await correct(db_session, account.user.id, message.id, spam, 3)  # type: ignore[arg-type]

    rows = (await db_session.scalars(select(TriageFeedback))).all()
    assert [(r.category_id, r.priority) for r in rows] == [(spam, 3)]


async def test_no_visible_category_is_a_permanent_error(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM, triage_settings: TriageSettings
) -> None:
    for category in await effective_categories(db_session, account.user.id):
        db_session.add(
            TriageCategoryPreference(user_id=account.user.id, category_id=category.id, hidden=True)
        )
    message = await account.message()

    with pytest.raises(StepError) as error:
        await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=triage_settings)

    assert (error.value.code, error.value.permanent) == ("triage_no_categories", True)


@pytest.mark.parametrize(
    ("failure", "code", "permanent"),
    [
        (CloudLLMDisabledError("cloud"), "llm_cloud_disabled", True),
        ("not json", "llm_output_invalid", False),
    ],
)
async def test_llm_failures_become_step_errors(
    db_session: AsyncSession,
    account: Account,
    fake_llm: FakeLLM,
    triage_settings: TriageSettings,
    failure: Exception | str,
    code: str,
    permanent: bool,
) -> None:
    message = await account.message()
    fake_llm.answer(*[failure] * 3)

    with pytest.raises(StepError) as error:
        await triage_message(db_session, message.id, llm=fake_llm.gateway, settings=triage_settings)

    assert (error.value.code, error.value.permanent) == (code, permanent)
    assert "Quarterly" not in str(error.value)

"""Counting and resetting step rows per mailbox (``app.processing.service``). Synthetic data."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import uuid7
from app.mail.models import Mailbox, MailboxType, Message
from app.processing import service
from app.processing.models import MessageProcessing, StepStatus
from app.processing.service import StepCounts
from tests.factories import make_user

pytestmark = pytest.mark.db


async def _mailbox(session: AsyncSession, owner_id: uuid.UUID) -> uuid.UUID:
    mailbox_id = uuid7()
    session.add(
        Mailbox(
            id=mailbox_id,
            type=MailboxType.IMAP,
            display_name="Test",
            address="test@example.org",
            owner_user_id=owner_id,
        )
    )
    await session.flush()
    return mailbox_id


async def _message(session: AsyncSession, mailbox_id: uuid.UUID, **steps: StepStatus) -> uuid.UUID:
    message_id = uuid7()
    session.add(Message(id=message_id, mailbox_id=mailbox_id, remote_ref=str(message_id)))
    await session.flush()
    for step, status in steps.items():
        session.add(
            MessageProcessing(
                id=uuid7(),
                message_id=message_id,
                step=step,
                version=1,
                status=status,
                error_code="llm_unavailable" if status == StepStatus.FAILED else None,
                attempts=3,
            )
        )
    await session.flush()
    return message_id


async def test_counts_steps_per_mailbox(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    first = await _mailbox(db_session, owner.id)
    second = await _mailbox(db_session, owner.id)
    empty = await _mailbox(db_session, owner.id)
    await _message(db_session, first, triage=StepStatus.FAILED, todos=StepStatus.PENDING)
    await _message(db_session, first, triage=StepStatus.RUNNING, todos=StepStatus.PENDING)
    await _message(db_session, first, triage=StepStatus.DONE, todos=StepStatus.DONE)
    await _message(db_session, second, triage=StepStatus.FAILED, todos=StepStatus.FAILED)

    counts = await service.count_steps_by_mailbox(db_session, [first, second, empty])

    assert counts == {
        first: StepCounts(pending=2, running=1, failed=1),
        second: StepCounts(failed=2),
    }
    assert counts.get(empty, StepCounts()) == StepCounts()
    assert await service.count_steps_by_mailbox(db_session, [second]) == {
        second: StepCounts(failed=2)
    }
    assert await service.count_steps_by_mailbox(db_session, []) == {}
    everything = await service.count_steps_by_mailbox(db_session)
    assert everything[first] == counts[first]


async def test_reset_failed_steps_of_one_mailbox(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    first = await _mailbox(db_session, owner.id)
    second = await _mailbox(db_session, owner.id)
    failed = await _message(db_session, first, triage=StepStatus.FAILED, index=StepStatus.DONE)
    await _message(db_session, first, triage=StepStatus.DONE)
    other = await _message(db_session, second, triage=StepStatus.FAILED)

    assert await service.reset_failed_steps(db_session, first) == [failed]

    rows = {
        (row.message_id, row.step): row
        for row in await db_session.scalars(
            select(MessageProcessing)
            .where(MessageProcessing.message_id.in_([failed, other]))
            .execution_options(populate_existing=True)
        )
    }
    triage = rows[(failed, "triage")]
    assert (triage.status, triage.error_code, triage.attempts) == (StepStatus.PENDING, None, 0)
    assert rows[(failed, "index")].status == StepStatus.DONE
    assert rows[(other, "triage")].status == StepStatus.FAILED


async def test_reset_failed_steps_skips_disabled_mailboxes(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    mailbox_id = await _mailbox(db_session, owner.id)
    await _message(db_session, mailbox_id, triage=StepStatus.FAILED)
    await service.set_mailbox_enabled(db_session, mailbox_id, False)

    assert await service.reset_failed_steps(db_session, mailbox_id) == []


async def test_counts_messages_with_skipped_steps_on_request(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    first = await _mailbox(db_session, owner.id)
    older = await _mailbox(db_session, owner.id)
    await _message(db_session, first, triage=StepStatus.PENDING, todos=StepStatus.SKIPPED)
    await _message(db_session, older, triage=StepStatus.SKIPPED, todos=StepStatus.SKIPPED)
    await _message(db_session, older, index=StepStatus.DONE, triage=StepStatus.SKIPPED)

    assert await service.count_steps_by_mailbox(db_session, [first, older]) == {
        first: StepCounts(pending=1, skipped_messages=1),
        older: StepCounts(skipped_messages=2),
    }
    assert await service.count_steps_by_mailbox(db_session, [first, older], skipped=False) == {
        first: StepCounts(pending=1)
    }


async def test_open_steps_are_counted_through_the_partial_index(db_session: AsyncSession) -> None:
    owner = await make_user(db_session)
    mailbox_id = await _mailbox(db_session, owner.id)
    await _message(db_session, mailbox_id, triage=StepStatus.FAILED)
    statements: list[Any] = []

    class Recording:
        async def execute(self, statement: Any) -> list[Any]:
            statements.append(statement)
            return []

    await service.count_steps_by_mailbox(Recording(), skipped=False)  # type: ignore[arg-type]
    compiled = statements[0].compile(dialect=db_session.bind.dialect)  # type: ignore[union-attr]
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    # A generic plan, as after a few executions of the prepared statement: the index
    # condition must be part of the SQL text, not a bound parameter.
    await db_session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    connection = await (await db_session.connection()).get_raw_connection()
    params = [compiled.params[name] for name in compiled.positiontup or ()]
    plan = await connection.driver_connection.fetch(  # type: ignore[union-attr]
        "EXPLAIN " + str(compiled), *params
    )

    assert "ix_message_processing_open" in "\n".join(row[0] for row in plan)

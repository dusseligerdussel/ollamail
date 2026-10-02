"""Counting and resetting step rows per mailbox (``app.processing.service``). Synthetic data."""

import uuid

import pytest
from sqlalchemy import select
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

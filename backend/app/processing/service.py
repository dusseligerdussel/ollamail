"""Database side of the pipeline: step state transitions and message selection.

The functions take an open session and never commit; ``app.processing.tasks`` decides
the transaction boundaries. All rows of one message are locked (``FOR UPDATE``) while
they are inspected, so concurrent steps of the same message see each other's results.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.ids import uuid7
from app.mail.models import Mailbox, Message
from app.processing.models import MailboxProcessingSettings, MessageProcessing, StepStatus
from app.processing.steps import ProcessingStep

PROCESSED_EVENT = "message.processed"


def _enabled_mailbox() -> ColumnElement[bool]:
    """SQL condition: ``Message.mailbox_id`` has processing enabled."""
    disabled = select(MailboxProcessingSettings.mailbox_id).where(
        MailboxProcessingSettings.enabled.is_(False)
    )
    return Message.mailbox_id.not_in(disabled)


async def is_mailbox_enabled(session: AsyncSession, mailbox_id: uuid.UUID) -> bool:
    enabled = await session.scalar(
        select(MailboxProcessingSettings.enabled).where(
            MailboxProcessingSettings.mailbox_id == mailbox_id
        )
    )
    return enabled is not False


async def set_mailbox_enabled(session: AsyncSession, mailbox_id: uuid.UUID, enabled: bool) -> None:
    statement = insert(MailboxProcessingSettings).values(
        id=uuid7(), mailbox_id=mailbox_id, enabled=enabled
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[MailboxProcessingSettings.mailbox_id],
            set_={"enabled": enabled, "updated_at": func.now()},
        )
    )


async def _lock_rows(session: AsyncSession, message_id: uuid.UUID) -> dict[str, MessageProcessing]:
    rows = await session.scalars(
        select(MessageProcessing)
        .where(MessageProcessing.message_id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return {row.step: row for row in rows}


def _ready(rows: dict[str, MessageProcessing], steps: Sequence[ProcessingStep]) -> list[str]:
    """Pending steps whose dependencies are all done."""
    return [
        step.name
        for step in steps
        if (row := rows.get(step.name)) is not None
        and row.status == StepStatus.PENDING
        and all(
            (dependency := rows.get(name)) is not None and dependency.status == StepStatus.DONE
            for name in step.depends_on
        )
    ]


def _reset(row: MessageProcessing, version: int) -> None:
    row.version = version
    row.status = StepStatus.PENDING
    row.error_code = None
    row.attempts = 0
    row.started_at = None
    row.finished_at = None


async def plan(
    session: AsyncSession, message_id: uuid.UUID, steps: Sequence[ProcessingStep]
) -> list[str]:
    """Bring the rows of a message up to the registered steps and versions.

    Missing steps and steps with an outdated version become ``pending``; done and failed
    steps at the current version are left alone. Returns the steps that can run now
    (empty if the message is gone or its mailbox has processing disabled).
    """
    mailbox_id = await session.scalar(select(Message.mailbox_id).where(Message.id == message_id))
    if mailbox_id is None or not await is_mailbox_enabled(session, mailbox_id) or not steps:
        return []
    await session.execute(
        insert(MessageProcessing)
        .values(
            [
                {
                    "id": uuid7(),
                    "message_id": message_id,
                    "step": step.name,
                    "version": step.version,
                    "status": StepStatus.PENDING,
                }
                for step in steps
            ]
        )
        .on_conflict_do_nothing(index_elements=["message_id", "step"])
    )
    rows = await _lock_rows(session, message_id)
    for step in steps:
        row = rows[step.name]
        if row.version != step.version:
            _reset(row, step.version)
    await session.flush()
    return _ready(rows, steps)


async def start_step(
    session: AsyncSession, message_id: uuid.UUID, step: ProcessingStep
) -> uuid.UUID | None:
    """Mark ``step`` as running if it is due; returns the mailbox ID, or ``None`` if the
    step must not run (already done or failed, dependencies not done, message gone,
    processing disabled)."""
    mailbox_id = await session.scalar(select(Message.mailbox_id).where(Message.id == message_id))
    if mailbox_id is None or not await is_mailbox_enabled(session, mailbox_id):
        return None
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None:
        return None
    if row.version != step.version:
        # Code with a newer version runs before the message was planned again.
        _reset(row, step.version)
    if row.status not in (StepStatus.PENDING, StepStatus.RUNNING):
        return None
    for name in step.depends_on:
        dependency = rows.get(name)
        if dependency is None or dependency.status != StepStatus.DONE:
            return None
    row.status = StepStatus.RUNNING
    row.attempts += 1
    row.started_at = datetime.now(UTC)
    row.finished_at = None
    await session.flush()
    return mailbox_id


async def _publish(
    session: AsyncSession, message_id: uuid.UUID, mailbox_id: uuid.UUID, status: str
) -> None:
    owner = await session.scalar(select(Mailbox.owner_user_id).where(Mailbox.id == mailbox_id))
    # TODO(#11): shared mailboxes notify their assigned users.
    if owner is None:
        return
    event = Event(
        type=PROCESSED_EVENT,
        ids={"message_id": message_id, "mailbox_id": mailbox_id},
        status=status,
    )
    await publish(session, owner, event)


async def finish_step(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    step: ProcessingStep,
    steps: Sequence[ProcessingStep],
) -> list[str]:
    """Mark ``step`` as done; returns the steps that can run next. Publishes
    ``message.processed`` (status ``done``) once all steps are done."""
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None:
        return []
    row.status = StepStatus.DONE
    row.version = step.version
    row.error_code = None
    row.finished_at = datetime.now(UTC)
    await session.flush()
    current = {(s.name, s.version) for s in steps}
    if all(
        (r := rows.get(s.name)) is not None
        and r.status == StepStatus.DONE
        and (r.step, r.version) in current
        for s in steps
    ):
        await _publish(session, message_id, mailbox_id, "done")
    return _ready(rows, steps)


async def fail_step(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    step: ProcessingStep,
    error_code: str,
    *,
    final: bool,
) -> None:
    """Record a failed attempt. ``final`` (no retry follows) marks the step ``failed``
    and publishes ``message.processed`` with status ``failed``."""
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None:
        return
    row.status = StepStatus.FAILED if final else StepStatus.PENDING
    row.error_code = error_code
    row.finished_at = datetime.now(UTC)
    await session.flush()
    if final:
        await _publish(session, message_id, mailbox_id, "failed")


def _message_time() -> ColumnElement[datetime]:
    return func.coalesce(Message.received_at, Message.sent_at, Message.created_at)


async def select_messages(
    session: AsyncSession,
    *,
    mailbox_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[uuid.UUID]:
    """IDs of messages in mailboxes with processing enabled, newest first."""
    query = select(Message.id).where(_enabled_mailbox())
    if mailbox_id is not None:
        query = query.where(Message.mailbox_id == mailbox_id)
    if since is not None:
        query = query.where(_message_time() >= since)
    if until is not None:
        query = query.where(_message_time() < until)
    return list(await session.scalars(query.order_by(Message.id.desc())))


async def reset_steps(
    session: AsyncSession, message_ids: Sequence[uuid.UUID], steps: Sequence[str] | None = None
) -> int:
    """Set the given steps (default: all) of the messages back to ``pending``."""
    if not message_ids:
        return 0
    statement = (
        update(MessageProcessing)
        .where(MessageProcessing.message_id.in_(message_ids))
        .values(
            status=StepStatus.PENDING,
            error_code=None,
            attempts=0,
            started_at=None,
            finished_at=None,
        )
    )
    if steps is not None:
        statement = statement.where(MessageProcessing.step.in_(steps))
    result = await session.execute(statement.returning(MessageProcessing.id))
    return len(result.all())


async def outdated_messages(
    session: AsyncSession, steps: Sequence[ProcessingStep], *, limit: int
) -> list[uuid.UUID]:
    """Messages (newest first) that lack a row for a registered step or have one for an
    older version: never planned, or a step's version was bumped."""
    if not steps:
        return []
    current = select(func.count()).where(
        MessageProcessing.message_id == Message.id,
        tuple_(MessageProcessing.step, MessageProcessing.version).in_(
            [(step.name, step.version) for step in steps]
        ),
    )
    query = (
        select(Message.id)
        .where(_enabled_mailbox(), current.scalar_subquery() < len(steps))
        .order_by(Message.id.desc())
        .limit(limit)
    )
    return list(await session.scalars(query))

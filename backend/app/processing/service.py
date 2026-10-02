"""Database side of the pipeline: step state transitions and message selection.

The functions take an open session and never commit; ``app.processing.tasks`` decides
the transaction boundaries. All rows of one message are locked (``FOR UPDATE``) while
they are inspected, so concurrent steps of the same message see each other's results.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.ids import uuid7
from app.mail.access import publish_to_readers
from app.mail.models import Mailbox, Message
from app.processing.models import MailboxProcessingSettings, MessageProcessing, StepStatus
from app.processing.steps import ProcessingStep, registry

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


# A predecessor in ``ProcessingStep.after`` no longer blocks once it is in one of these.
_SETTLED = (StepStatus.DONE, StepStatus.FAILED)


def _unblocked(
    rows: dict[str, MessageProcessing], step: ProcessingStep, registered: Collection[str]
) -> bool:
    """All ``depends_on`` steps are done and all registered ``after`` steps settled."""
    return all(
        (dependency := rows.get(name)) is not None and dependency.status == StepStatus.DONE
        for name in step.depends_on
    ) and all(
        (predecessor := rows.get(name)) is None or predecessor.status in _SETTLED
        for name in step.after
        if name in registered
    )


def _ready(rows: dict[str, MessageProcessing], steps: Sequence[ProcessingStep]) -> list[str]:
    """Pending steps whose dependencies are all done (``after`` steps: settled)."""
    registered = {step.name for step in steps}
    return [
        step.name
        for step in steps
        if (row := rows.get(step.name)) is not None
        and row.status == StepStatus.PENDING
        and _unblocked(rows, step, registered)
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
    registered = {name for name in step.after if registry.get(name) is not None}
    if not _unblocked(rows, step, registered):
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
    event = Event(
        type=PROCESSED_EVENT,
        ids={"message_id": message_id, "mailbox_id": mailbox_id},
        status=status,
    )
    owner = await session.scalar(select(Mailbox.owner_user_id).where(Mailbox.id == mailbox_id))
    if owner is not None:
        await publish(session, owner, event)
    else:
        # Shared mailbox: its assigned users.
        await publish_to_readers(session, mailbox_id, event)


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
    steps: Sequence[ProcessingStep] = (),
) -> list[str]:
    """Record a failed attempt. ``final`` (no retry follows) marks the step ``failed``
    and publishes ``message.processed`` with status ``failed``. Returns the steps of
    ``steps`` that can run now (steps waiting for the failed one via ``after``)."""
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None:
        return []
    row.status = StepStatus.FAILED if final else StepStatus.PENDING
    row.error_code = error_code
    row.finished_at = datetime.now(UTC)
    await session.flush()
    if not final:
        return []
    await _publish(session, message_id, mailbox_id, "failed")
    return _ready(rows, steps)


@dataclass(frozen=True)
class StepCounts:
    """Step rows of one mailbox by state; counts only, no message IDs or content."""

    # Not run yet: waiting for dependencies, a job or a retry.
    pending: int = 0
    running: int = 0
    # Gave up; ``reset_failed_steps`` (or an automatic retry) runs them again.
    failed: int = 0


async def count_steps_by_mailbox(
    session: AsyncSession, mailbox_ids: Collection[uuid.UUID] | None = None
) -> dict[uuid.UUID, StepCounts]:
    """Pending, running and failed steps per mailbox (default: all mailboxes).

    Mailboxes without such steps are missing from the result; use ``StepCounts()`` as
    the default. Done steps are not counted.
    """
    query = (
        select(Message.mailbox_id, MessageProcessing.status, func.count())
        .join(Message, Message.id == MessageProcessing.message_id)
        .where(MessageProcessing.status != StepStatus.DONE)
        .group_by(Message.mailbox_id, MessageProcessing.status)
    )
    if mailbox_ids is not None:
        if not mailbox_ids:
            return {}
        query = query.where(Message.mailbox_id.in_(mailbox_ids))
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for mailbox_id, status, count in await session.execute(query):
        counts.setdefault(mailbox_id, {})[StepStatus(status).value] = count
    return {mailbox_id: StepCounts(**values) for mailbox_id, values in counts.items()}


async def reset_failed_steps(
    session: AsyncSession, mailbox_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Set all failed steps (of one mailbox, default: all) back to ``pending``; returns
    the IDs of the affected messages, which the caller queues again
    (``app.processing.tasks.requeue_messages``). Mailboxes with processing disabled
    are left out."""
    failed = select(MessageProcessing.message_id).where(
        MessageProcessing.status == StepStatus.FAILED
    )
    query = (
        select(Message.id)
        .where(_enabled_mailbox(), Message.id.in_(failed))
        .order_by(Message.id.desc())
    )
    if mailbox_id is not None:
        query = query.where(Message.mailbox_id == mailbox_id)
    message_ids = list(await session.scalars(query))
    if message_ids:
        await session.execute(
            update(MessageProcessing)
            .where(
                MessageProcessing.message_id.in_(message_ids),
                MessageProcessing.status == StepStatus.FAILED,
            )
            .values(
                status=StepStatus.PENDING,
                error_code=None,
                attempts=0,
                started_at=None,
                finished_at=None,
            )
        )
    return message_ids


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

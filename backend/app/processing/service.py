"""Database side of the pipeline: step state transitions and message selection.

The functions take an open session and never commit; ``app.processing.tasks`` decides
the transaction boundaries. All rows of one message are locked (``FOR UPDATE``) while
they are inspected, so concurrent steps of the same message see each other's results.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, exists, func, select, text, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.ids import uuid7, uuid7_floor, uuid7_time
from app.mail.access import publish_to_readers
from app.mail.models import Mailbox, Message
from app.processing.models import (
    OPEN_STEPS,
    MailboxProcessingSettings,
    MessageProcessing,
    ProcessingScanState,
    StepStatus,
)
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


async def _mailbox_flags(session: AsyncSession, mailbox_id: uuid.UUID) -> tuple[bool, bool]:
    """``(enabled, include_older)`` of a mailbox; defaults if it has no settings row."""
    row = (
        await session.execute(
            select(
                MailboxProcessingSettings.enabled, MailboxProcessingSettings.include_older
            ).where(MailboxProcessingSettings.mailbox_id == mailbox_id)
        )
    ).first()
    return (True, False) if row is None else (row[0], row[1])


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
    if enabled:
        # Its mails were left out while disabled: the next ``requeue_outdated`` checks
        # all messages again and queues the ones missed meanwhile.
        await session.execute(
            update(ProcessingScanState)
            .where(ProcessingScanState.key == SCAN_STATE_KEY)
            .values(step_versions=None)
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
_SETTLED = (StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED)
# A message is processed once all its steps are in one of these.
_COMPLETE = (StepStatus.DONE, StepStatus.SKIPPED)


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
    row.retry_at = None
    row.auto_retries = 0


# Column values of a step set back to ``pending`` by hand (reprocessing).
_RESET_VALUES = {
    "status": StepStatus.PENDING,
    "error_code": None,
    "attempts": 0,
    "started_at": None,
    "finished_at": None,
    "retry_at": None,
    "auto_retries": 0,
}


async def plan(
    session: AsyncSession,
    message_id: uuid.UUID,
    steps: Sequence[ProcessingStep],
    *,
    recent_since: datetime | None = None,
) -> list[str]:
    """Bring the rows of a message up to the registered steps and versions.

    Missing steps and steps with an outdated version become ``pending``; done and failed
    steps at the current version are left alone. If the message was received before
    ``recent_since`` and its mailbox did not opt in (``include_older``), pending
    ``recent_only`` steps and the steps depending on them become ``skipped``; otherwise
    skipped steps become ``pending`` again. Returns the steps that can run now (empty if
    the message is gone or its mailbox has processing disabled).
    """
    found = (
        await session.execute(
            select(Message.mailbox_id, _message_time()).where(Message.id == message_id)
        )
    ).first()
    if found is None or not steps:
        return []
    mailbox_id, received = found
    enabled, include_older = await _mailbox_flags(session, mailbox_id)
    if not enabled:
        return []
    old = recent_since is not None and not include_older and received < recent_since
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
    skipped: set[str] = set()
    # ``steps`` is ordered (dependencies first), so a skipped dependency is known here.
    for step in steps:
        row = rows[step.name]
        if row.version != step.version:
            _reset(row, step.version)
        skip = old and (step.recent_only or any(name in skipped for name in step.depends_on))
        if skip and row.status == StepStatus.PENDING:
            row.status = StepStatus.SKIPPED
        elif not skip and row.status == StepStatus.SKIPPED:
            _reset(row, step.version)
        if row.status == StepStatus.SKIPPED:
            skipped.add(step.name)
    await session.flush()
    return _ready(rows, steps)


async def fail_plan(
    session: AsyncSession,
    message_id: uuid.UUID,
    steps: Sequence[ProcessingStep],
    error_code: str,
) -> bool:
    """Record that planning a message failed for good (its plan job gave up): missing
    steps and steps with an outdated version become ``failed`` at the current version
    with ``error_code``. Otherwise ``processing.requeue_outdated`` would find the message
    again every 10 minutes. Like other failed steps, they are shown in the admin
    overview and run again by reprocessing. ``False`` if the message is gone."""
    found = await session.scalar(
        select(Message.id).where(Message.id == message_id).with_for_update(key_share=True)
    )
    if found is None or not steps:
        return False
    now = datetime.now(UTC)
    await session.execute(
        insert(MessageProcessing)
        .values(
            [
                {
                    "id": uuid7(),
                    "message_id": message_id,
                    "step": step.name,
                    "version": step.version,
                    "status": StepStatus.FAILED,
                    "error_code": error_code,
                    "finished_at": now,
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
            row.status = StepStatus.FAILED
            row.error_code = error_code
            row.finished_at = now
    await session.flush()
    return True


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
    row.retry_at = None
    row.auto_retries = 0
    await session.flush()
    current = {(s.name, s.version) for s in steps}
    if all(
        (r := rows.get(s.name)) is not None
        and r.status in _COMPLETE
        and (r.step, r.version) in current
        for s in steps
    ):
        await _publish(session, message_id, mailbox_id, "done")
    return _ready(rows, steps)


@dataclass(frozen=True)
class AutoRetry:
    """Automatic retries of a step that failed for a passing reason: at most ``limit``,
    the first after ``delay``, then twice as long each time (at most ``MAX_RETRY_DELAY``)."""

    limit: int
    delay: timedelta


MAX_RETRY_DELAY = timedelta(days=1)


def retry_delay(auto_retry: AutoRetry, retries: int) -> timedelta:
    """Wait before automatic retry number ``retries + 1``."""
    return min(auto_retry.delay * (1 << min(retries, 16)), MAX_RETRY_DELAY)


async def fail_step(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    step: ProcessingStep,
    error_code: str,
    *,
    final: bool,
    steps: Sequence[ProcessingStep] = (),
    auto_retry: AutoRetry | None = None,
) -> list[str]:
    """Record a failed attempt. ``final`` (no retry follows) marks the step ``failed``
    and publishes ``message.processed`` with status ``failed``; with ``auto_retry``
    (a passing error) it also sets ``retry_at`` unless the automatic retries are used
    up. Returns the steps of ``steps`` that can run now (steps waiting for the failed
    one via ``after``)."""
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None:
        return []
    now = datetime.now(UTC)
    row.status = StepStatus.FAILED if final else StepStatus.PENDING
    row.error_code = error_code
    row.finished_at = now
    row.retry_at = None
    if final and auto_retry is not None and row.auto_retries < auto_retry.limit:
        row.retry_at = now + retry_delay(auto_retry, row.auto_retries)
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
    # Of ``failed``: an automatic retry is scheduled (passing error, e.g. LLM down).
    retry_scheduled: int = 0
    # Messages (not steps) with skipped steps: older than the backfill window
    # (``OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS``); ``include_older`` processes them.
    skipped_messages: int = 0


async def count_steps_by_mailbox(
    session: AsyncSession,
    mailbox_ids: Collection[uuid.UUID] | None = None,
    *,
    skipped: bool = True,
) -> dict[uuid.UUID, StepCounts]:
    """Pending, running and failed steps and messages with skipped steps per mailbox
    (default: all mailboxes).

    Mailboxes without such steps are missing from the result; use ``StepCounts()`` as
    the default. Done steps are not counted. Open steps are few and read through a
    partial index; skipped ones exist for every mail outside the backfill window, so
    counting them reads all of those (``skipped=False`` leaves them out, e.g. metrics).
    """
    if mailbox_ids is not None and not mailbox_ids:
        return {}
    open_steps = (
        select(MessageProcessing.message_id, MessageProcessing.status, MessageProcessing.retry_at)
        .where(text(OPEN_STEPS))
        .subquery()
    )
    status = open_steps.c.status
    query = (
        select(
            Message.mailbox_id,
            func.count().filter(status == StepStatus.PENDING),
            func.count().filter(status == StepStatus.RUNNING),
            func.count().filter(status == StepStatus.FAILED),
            func.count().filter(status == StepStatus.FAILED, open_steps.c.retry_at.is_not(None)),
        )
        .join(open_steps, open_steps.c.message_id == Message.id)
        .group_by(Message.mailbox_id)
    )
    if mailbox_ids is not None:
        query = query.where(Message.mailbox_id.in_(mailbox_ids))
    result = {
        mailbox_id: StepCounts(*counts) for mailbox_id, *counts in await session.execute(query)
    }
    if not skipped:
        return result
    skipped_query = (
        select(Message.mailbox_id, func.count())
        .where(
            Message.id.in_(
                select(MessageProcessing.message_id).where(
                    MessageProcessing.status == StepStatus.SKIPPED
                )
            )
        )
        .group_by(Message.mailbox_id)
    )
    if mailbox_ids is not None:
        skipped_query = skipped_query.where(Message.mailbox_id.in_(mailbox_ids))
    for mailbox_id, count in await session.execute(skipped_query):
        result[mailbox_id] = replace(result.get(mailbox_id, StepCounts()), skipped_messages=count)
    return result


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
            .values(**_RESET_VALUES)
        )
    return message_ids


async def include_older(session: AsyncSession, mailbox_id: uuid.UUID) -> list[uuid.UUID]:
    """ "Classify older mails too": from now on, run ``recent_only`` steps for all mails of
    the mailbox, and set its skipped steps back to ``pending``. Returns the IDs of the
    affected messages (most recently received first), which the caller queues again
    (``app.processing.tasks.requeue_messages``). Empty if processing is disabled."""
    statement = insert(MailboxProcessingSettings).values(
        id=uuid7(), mailbox_id=mailbox_id, include_older=True
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[MailboxProcessingSettings.mailbox_id],
            set_={"include_older": True, "updated_at": func.now()},
        )
    )
    skipped = select(MessageProcessing.message_id).where(
        MessageProcessing.status == StepStatus.SKIPPED
    )
    message_ids = list(
        await session.scalars(
            select(Message.id)
            .where(Message.mailbox_id == mailbox_id, _enabled_mailbox(), Message.id.in_(skipped))
            .order_by(_message_time().desc(), Message.id.desc())
        )
    )
    if message_ids:
        await session.execute(
            update(MessageProcessing)
            .where(
                MessageProcessing.message_id.in_(message_ids),
                MessageProcessing.status == StepStatus.SKIPPED,
            )
            .values(**_RESET_VALUES)
        )
    return message_ids


async def postpone_step(session: AsyncSession, message_id: uuid.UUID, step: ProcessingStep) -> None:
    """Undo ``start_step``: the step did not really run (the LLM endpoint is paused), so
    it waits again without using up an attempt."""
    rows = await _lock_rows(session, message_id)
    row = rows.get(step.name)
    if row is None or row.status != StepStatus.RUNNING:
        return
    row.status = StepStatus.PENDING
    row.attempts = max(0, row.attempts - 1)
    row.started_at = None
    await session.flush()


async def requeue_due_retries(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Set failed steps whose ``retry_at`` has come back to ``pending`` (oldest due
    first, at most ``limit`` steps) and count the automatic retry. Returns the IDs of
    the affected messages, which the caller queues again. Mailboxes with processing
    disabled are left out; their steps stay due until processing is enabled."""
    due = (
        select(MessageProcessing.id)
        .join(Message, Message.id == MessageProcessing.message_id)
        .where(
            MessageProcessing.status == StepStatus.FAILED,
            MessageProcessing.retry_at <= func.now(),
            _enabled_mailbox(),
        )
        .order_by(MessageProcessing.retry_at)
        .limit(limit)
    )
    result = await session.execute(
        update(MessageProcessing)
        .where(MessageProcessing.id.in_(due.scalar_subquery()))
        .values(
            status=StepStatus.PENDING,
            attempts=0,
            started_at=None,
            finished_at=None,
            retry_at=None,
            auto_retries=MessageProcessing.auto_retries + 1,
        )
        .returning(MessageProcessing.message_id)
    )
    return list(dict.fromkeys(result.scalars()))


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
        .values(**_RESET_VALUES)
    )
    if steps is not None:
        statement = statement.where(MessageProcessing.step.in_(steps))
    result = await session.execute(statement.returning(MessageProcessing.id))
    return len(result.all())


async def outdated_messages(
    session: AsyncSession,
    steps: Sequence[ProcessingStep],
    *,
    limit: int,
    before: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Messages (newest first, only IDs below ``before`` if given) that lack a row for a
    registered step or have one for an older version: never planned, or a step's
    version was bumped. Reads every message up to ``limit`` hits; ``messages_to_requeue``
    runs it only when needed."""
    if not steps:
        return []
    current = select(func.count()).where(
        MessageProcessing.message_id == Message.id,
        tuple_(MessageProcessing.step, MessageProcessing.version).in_(
            [(step.name, step.version) for step in steps]
        ),
    )
    query = select(Message.id).where(_enabled_mailbox(), current.scalar_subquery() < len(steps))
    if before is not None:
        query = query.where(Message.id < before)
    return list(await session.scalars(query.order_by(Message.id.desc()).limit(limit)))


SCAN_STATE_KEY = "requeue"
# Messages stored this long before the last check are checked again: their IDs are
# taken before the insert, and the transaction may commit after a later one.
PLANNED_MARGIN = timedelta(hours=1)


async def messages_to_requeue(
    session: AsyncSession, steps: Sequence[ProcessingStep], *, limit: int
) -> list[uuid.UUID]:
    """Messages (newest first, at most ``limit``) that ``processing.requeue_outdated``
    queues again: never planned, or planned with other steps or versions. Does not
    commit; the caller commits the updated ``ProcessingScanState``.

    Usually nothing is outdated, so this avoids reading all messages: after a check of
    all messages found none (``outdated_messages``), it remembers the step versions and
    from then on only checks the messages stored since (by their UUIDv7 IDs) for
    missing rows. Steps or versions that differ from the remembered ones (an update,
    a new feature, a mailbox enabled again) lead to a check of all messages again.
    Relies on ``plan`` creating the rows of all steps of a message at once.

    The check of all messages walks down the IDs with a keyset cursor
    (``ProcessingScanState.cursor``): after a version bump each run queues the next
    ``limit`` outdated messages below the previous ones instead of reading again from
    the newest, so all messages are read once and not once per batch. At the oldest
    message it starts over from the newest; only a check that finds nothing marks the
    versions as done. Messages whose plan job failed for good are marked ``failed``
    (``fail_plan``) and are not queued again.
    """
    if not steps:
        return []
    current = {step.name: step.version for step in steps}
    await session.execute(
        insert(ProcessingScanState)
        .values(id=uuid7(), key=SCAN_STATE_KEY)
        .on_conflict_do_nothing(index_elements=[ProcessingScanState.key])
    )
    state = await session.scalar(
        select(ProcessingScanState)
        .where(ProcessingScanState.key == SCAN_STATE_KEY)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert state is not None
    # Transaction start: everything committed before is visible to the checks below.
    started = await session.scalar(select(func.now()))
    assert started is not None
    if state.step_versions != current or state.planned_before is None:
        message_ids = await outdated_messages(session, steps, limit=limit, before=state.cursor)
        if len(message_ids) == limit:
            # More may follow: the next run continues below the last one.
            state.cursor = message_ids[-1]
        elif state.cursor is not None:
            # Reached the oldest message. The next run checks from the newest again and
            # only finds what the queued plan jobs have not brought up to date yet.
            state.cursor = None
        elif not message_ids:
            state.step_versions = current
            state.planned_before = started
        await session.flush()
        return message_ids

    unplanned = (
        select(Message.id)
        .where(
            Message.id >= uuid7_floor(state.planned_before - PLANNED_MARGIN),
            ~exists().where(MessageProcessing.message_id == Message.id),
            _enabled_mailbox(),
        )
        .order_by(Message.id.desc())
        .limit(limit)
    )
    message_ids = list(await session.scalars(unplanned))
    if not message_ids:
        state.planned_before = started
    elif len(message_ids) < limit:
        # Keep checking from the oldest message still unplanned (its plan job is queued
        # or failed); everything before it is planned.
        state.planned_before = max(state.planned_before, uuid7_time(message_ids[-1]))
    await session.flush()
    return message_ids

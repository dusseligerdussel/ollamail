"""Enforcing the retention periods (daily job ``privacy.retention``).

Hard delete, in batches, with the files after each commit:

* mails older than ``mail_days`` (age: received, else sent, else imported) with their
  attachments, search index, triage results, processing state and citations (cascade);
  threads left without mails are removed too
* attachments older than ``attachment_days`` (the mail stays)
* the search index (chunks and embeddings) of mails older than ``search_index_days``
* audit events older than ``audit_days`` through the database function
  ``audit_events_purge`` (migration ``add_privacy``), the only way past the append-only
  trigger. It removes the oldest events as one contiguous block, so the hash chain stays
  verifiable from the first remaining event; the ``data.deleted`` entry of the run records
  that event's ID and its ``prev_hash`` as the new starting point.

Digests and conversations are enforced by their own jobs (``digest.cleanup``,
``rag.purge_conversations``) with the same effective periods (``app.privacy.policy``).
Each run that deletes something writes one ``data.deleted`` audit entry with counts.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, delete, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.audit.models import audit_events
from app.core.config import Settings
from app.core.logging import get_logger
from app.mail.models import Attachment, Message, Thread
from app.mail.storage import AttachmentStorage
from app.privacy.models import RetentionSettingsRecord
from app.privacy.policy import RetentionPolicy, effective_policy, get_record
from app.search.models import SearchChunk

log = get_logger(__name__)

BATCH_SIZE = 500


@dataclass
class RetentionResult:
    mails: int = 0
    attachments: int = 0
    search_chunks: int = 0
    threads: int = 0
    audit_events: int = 0

    def any(self) -> bool:
        return any(asdict(self).values())


def message_age() -> ColumnElement[datetime]:
    return func.coalesce(Message.received_at, Message.sent_at, Message.created_at)


def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)


async def _delete_files(storage: AttachmentStorage, paths: Sequence[str]) -> None:
    for path in paths:
        try:
            await asyncio.to_thread(storage.delete, path)
        except ValueError:
            # A stored path outside the attachment directory is never touched.
            log.warning("privacy_retention_invalid_path")


async def purge_messages(
    session: AsyncSession, storage: AttachmentStorage, cutoff: datetime
) -> tuple[int, int]:
    """Delete mails older than ``cutoff`` and the threads left empty. Commits per batch."""
    messages = 0
    mailboxes: set[uuid.UUID] = set()
    while True:
        rows = (
            await session.execute(
                select(Message.id, Message.mailbox_id)
                .where(message_age() < cutoff)
                .order_by(Message.id)
                .limit(BATCH_SIZE)
            )
        ).all()
        if not rows:
            break
        ids = [row.id for row in rows]
        mailboxes.update(row.mailbox_id for row in rows)
        paths = list(
            await session.scalars(
                select(Attachment.storage_path).where(Attachment.message_id.in_(ids))
            )
        )
        result = await session.execute(delete(Message).where(Message.id.in_(ids)))
        await session.commit()
        await _delete_files(storage, paths)
        messages += int(result.rowcount)  # type: ignore[attr-defined]
    threads = 0
    if mailboxes:
        result = await session.execute(
            delete(Thread).where(
                Thread.mailbox_id.in_(mailboxes),
                Thread.last_message_at < cutoff,
                ~exists().where(Message.thread_id == Thread.id),
            )
        )
        await session.commit()
        threads = int(result.rowcount)  # type: ignore[attr-defined]
    return messages, threads


async def purge_attachments(
    session: AsyncSession, storage: AttachmentStorage, cutoff: datetime
) -> int:
    """Delete attachments (rows and files) of mails older than ``cutoff``."""
    deleted = 0
    while True:
        rows = (
            await session.execute(
                select(Attachment.id, Attachment.storage_path)
                .join(Message, Message.id == Attachment.message_id)
                .where(message_age() < cutoff)
                .order_by(Attachment.id)
                .limit(BATCH_SIZE)
            )
        ).all()
        if not rows:
            return deleted
        await session.execute(delete(Attachment).where(Attachment.id.in_([r.id for r in rows])))
        await session.commit()
        await _delete_files(storage, [row.storage_path for row in rows])
        deleted += len(rows)


async def purge_search_index(session: AsyncSession, cutoff: datetime) -> int:
    """Delete the search chunks (and with them the embeddings) of mails older than
    ``cutoff``. The processing state stays "done", so they are not indexed again."""
    deleted = 0
    while True:
        ids = list(
            await session.scalars(
                select(SearchChunk.id)
                .join(Message, Message.id == SearchChunk.message_id)
                .where(message_age() < cutoff)
                .limit(BATCH_SIZE * 4)
            )
        )
        if not ids:
            return deleted
        await session.execute(delete(SearchChunk).where(SearchChunk.id.in_(ids)))
        await session.commit()
        deleted += len(ids)


async def purge_audit(session: AsyncSession, cutoff: datetime) -> int:
    """Delete audit events older than ``cutoff`` (oldest first, contiguous) through the
    documented exception to the append-only trigger. Does not commit."""
    deleted = await session.scalar(text("SELECT audit_events_purge(:cutoff)"), {"cutoff": cutoff})
    return int(deleted or 0)


async def _chain_start(session: AsyncSession) -> tuple[int, bytes | None] | None:
    row = (
        await session.execute(
            select(audit_events.c.id, audit_events.c.prev_hash).order_by(audit_events.c.id).limit(1)
        )
    ).first()
    return None if row is None else (row.id, row.prev_hash)


async def _save_run(session: AsyncSession, now: datetime, result: RetentionResult) -> None:
    record = await get_record(session)
    if record is None:
        record = RetentionSettingsRecord()
        session.add(record)
    record.last_run_at = now
    record.last_run = asdict(result)


async def enforce(
    session: AsyncSession,
    policy: RetentionPolicy,
    storage: AttachmentStorage,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    """Apply ``policy`` to mails, attachments, search index and audit log. Commits."""
    now = now or datetime.now(UTC)
    result = RetentionResult()
    if policy.mail_days > 0:
        result.mails, result.threads = await purge_messages(
            session, storage, _cutoff(now, policy.mail_days)
        )
    if policy.attachment_days > 0:
        result.attachments = await purge_attachments(
            session, storage, _cutoff(now, policy.attachment_days)
        )
    if policy.search_index_days > 0:
        result.search_chunks = await purge_search_index(
            session, _cutoff(now, policy.search_index_days)
        )
    details: dict[str, int | str | None] = {}
    if policy.audit_days > 0:
        result.audit_events = await purge_audit(session, _cutoff(now, policy.audit_days))
        if result.audit_events:
            start = await _chain_start(session)
            if start is not None:
                details["chain_start_id"] = start[0]
                details["chain_start_prev_hash"] = start[1].hex() if start[1] else None
    if result.any():
        await audit.record(
            session,
            audit.SYSTEM,
            audit.AuditAction.DATA_DELETED,
            None,
            {"reason": "retention", **asdict(result), **details},
        )
    await _save_run(session, now, result)
    await session.commit()
    log.info("privacy_retention_finished", **asdict(result))
    return result


async def run(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> RetentionResult:
    policy = await effective_policy(session, settings)
    return await enforce(session, policy, AttachmentStorage(settings.storage.data_dir), now=now)

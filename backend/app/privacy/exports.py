"""Export requests: create, run (worker), list, download and expire.

A user has at most one export in progress. A finished export can be downloaded by its
owner until ``expires_at`` (``OLLAMAIL_PRIVACY_EXPORT_EXPIRY_HOURS``); then the hourly job
``privacy.cleanup_exports`` deletes the row and the file.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.core.events import Event, publish
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.privacy import export
from app.privacy.models import DataExport, ExportStatus
from app.privacy.storage import ExportStorage
from app.processing.tasks import error_code

log = get_logger(__name__)

IN_PROGRESS = (ExportStatus.PENDING, ExportStatus.RUNNING)
# An export still "running" after this long was interrupted (worker restart).
STALLED_AFTER = timedelta(hours=2)


class ExportNotFoundError(Exception):
    pass


async def list_exports(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> list[DataExport]:
    """The user's exports that are not expired, newest first."""
    now = now or datetime.now(UTC)
    return list(
        await session.scalars(
            select(DataExport)
            .where(
                DataExport.user_id == user_id,
                (DataExport.expires_at.is_(None)) | (DataExport.expires_at > now),
            )
            .order_by(DataExport.created_at.desc())
        )
    )


async def get_export(
    session: AsyncSession, user_id: uuid.UUID, export_id: uuid.UUID
) -> DataExport | None:
    """An export of ``user_id``; another user's export is reported as missing."""
    return await session.scalar(
        select(DataExport).where(DataExport.id == export_id, DataExport.user_id == user_id)
    )


async def request_export(
    session: AsyncSession, user_id: uuid.UUID, *, expiry_hours: int
) -> tuple[DataExport, bool]:
    """Create an export (caller commits and queues it), or return the one in progress.
    Returns the export and whether it is new."""
    running = await session.scalar(
        select(DataExport)
        .where(DataExport.user_id == user_id, DataExport.status.in_(IN_PROGRESS))
        .with_for_update()
    )
    if running is not None:
        return running, False
    item = DataExport(
        user_id=user_id,
        status=ExportStatus.PENDING,
        # Until it is ready: a pending export that never runs disappears as well.
        expires_at=datetime.now(UTC) + timedelta(hours=expiry_hours) + STALLED_AFTER,
    )
    session.add(item)
    await session.flush()
    await audit.record(
        session,
        audit.Actor.user(user_id),
        audit.AuditAction.DATA_EXPORTED,
        audit.Target.of(audit.TargetType.USER, user_id),
        {"export_id": item.id, "stage": "requested"},
    )
    return item, True


async def _publish(session: AsyncSession, item: DataExport) -> None:
    await publish(
        session,
        item.user_id,
        Event(type="privacy.export", ids={"export_id": item.id}, status=item.status.value),
    )


async def run_export(
    session: AsyncSession,
    export_id: uuid.UUID,
    *,
    storage: ExportStorage,
    digest_storage: DigestStorage,
    expiry_hours: int,
) -> None:
    """Build the ZIP of a pending export (worker). Commits; idempotent."""
    item = await session.get(DataExport, export_id)
    if item is None or item.status not in IN_PROGRESS:
        return
    item.status = ExportStatus.RUNNING
    await _publish(session, item)
    await session.commit()
    # A rollback expires ``item``; keep what is needed afterwards.
    user_id, created_at = item.user_id, item.created_at
    target = storage.path(user_id, export_id)
    try:
        size = await export.build(
            session,
            user_id,
            target,
            export_id=export_id,
            created_at=created_at,
            digest_storage=digest_storage,
        )
    except export.UserNotFoundError:
        await session.rollback()
        await asyncio.to_thread(storage.delete_export, user_id, export_id)
        return
    except Exception as exc:
        await session.rollback()
        await asyncio.to_thread(storage.delete_export, user_id, export_id)
        await mark_failed(session, export_id, error_code(exc))
        raise
    now = datetime.now(UTC)
    item = await session.get(DataExport, export_id, populate_existing=True)
    if item is None:
        # Deleted meanwhile (user or export): do not leave the file behind.
        await asyncio.to_thread(target.unlink, True)
        return
    item.status = ExportStatus.READY
    item.file_path = storage.relative(target)
    item.size = size
    item.finished_at = now
    item.expires_at = now + timedelta(hours=expiry_hours)
    await _publish(session, item)
    await session.commit()
    log.info("privacy_export_ready", export_id=str(export_id), size=size)


async def mark_failed(session: AsyncSession, export_id: uuid.UUID, code: str) -> None:
    item = await session.get(DataExport, export_id)
    if item is None:
        return
    item.status = ExportStatus.FAILED
    item.error_code = code[:64]
    await _publish(session, item)
    await session.commit()
    log.warning("privacy_export_failed", export_id=str(export_id), error_code=item.error_code)


async def delete_export(session: AsyncSession, storage: ExportStorage, item: DataExport) -> None:
    """Delete an export and its file. Commits."""
    user_id, export_id = item.user_id, item.id
    await session.delete(item)
    await session.commit()
    await asyncio.to_thread(storage.delete_export, user_id, export_id)


@dataclass(frozen=True)
class ExportCleanup:
    expired: int
    stalled: int
    files: int


async def cleanup_exports(
    session: AsyncSession, storage: ExportStorage, *, now: datetime
) -> ExportCleanup:
    """Delete expired exports with their files, fail stalled ones and remove files that
    belong to no export (e.g. after the user was deleted). Commits."""
    expired = (
        await session.execute(
            delete(DataExport)
            .where(DataExport.expires_at <= now)
            .returning(DataExport.user_id, DataExport.id)
        )
    ).all()
    stalled = (
        await session.scalars(
            select(DataExport.id).where(
                DataExport.status.in_(IN_PROGRESS), DataExport.updated_at < now - STALLED_AFTER
            )
        )
    ).all()
    for export_id in stalled:
        item = await session.get(DataExport, export_id)
        if item is not None:
            item.status = ExportStatus.FAILED
            item.error_code = "stalled"
    await session.commit()
    for user_id, export_id in expired:
        await asyncio.to_thread(storage.delete_export, user_id, export_id)

    files = 0
    for user_id in await asyncio.to_thread(storage.user_ids):
        known = set(
            await session.scalars(select(DataExport.id).where(DataExport.user_id == user_id))
        )
        if not known:
            await asyncio.to_thread(storage.delete_user, user_id)
            files += 1
            continue
        for export_id in await asyncio.to_thread(storage.export_ids, user_id) - known:
            await asyncio.to_thread(storage.delete_export, user_id, export_id)
            files += 1
    await session.commit()
    result = ExportCleanup(len(expired), len(stalled), files)
    if any((result.expired, result.stalled, result.files)):
        log.info(
            "privacy_exports_cleaned",
            expired=result.expired,
            stalled=result.stalled,
            files=result.files,
        )
    return result

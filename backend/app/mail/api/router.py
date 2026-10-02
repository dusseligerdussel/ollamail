"""Mailbox API: users add, test, configure, monitor and remove their own mailboxes.

Access goes through ``app.mail.api.access`` (owner only until shared mailboxes, #34);
another user's mailbox answers 404. Connection tests use the provider registry, syncing
the existing sync job (``app.mail.sync``), removal ``app.mail.service.delete_mailbox``.
Progress arrives as SSE events: ``mailbox.sync`` from the sync (``progress``, ``done``,
``failed``) and ``mailbox.changed`` from this API (``created``, ``updated``, ``deleted``).
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.jobs import JobQueue
from app.core.logging import get_logger
from app.mail import service as mail_service
from app.mail.api import access, autodiscovery, service
from app.mail.api.access import MailboxPermission
from app.mail.api.schemas import (
    AutodiscoverRequest,
    AutodiscoverResult,
    AutodiscoverSuggestion,
    ConnectionTestResult,
    FolderRead,
    FolderSelectionUpdate,
    MailboxConnection,
    MailboxCreate,
    MailboxDeleted,
    MailboxRead,
    MailboxSyncStatus,
    MailboxUpdate,
    SyncRequestResult,
)
from app.mail.models import Mailbox, MailboxType
from app.mail.providers.registry import ProviderRegistry, registry
from app.mail.storage import AttachmentStorage
from app.mail.sync.tasks import request_sync

log = get_logger(__name__)

router = APIRouter(
    prefix="/mailboxes",
    tags=["mailboxes"],
    responses={401: {"description": "Not signed in"}},
)

SyncRequester = Callable[[uuid.UUID], Awaitable[bool]]


def get_provider_registry() -> ProviderRegistry:
    return registry


def get_attachment_storage(settings: SettingsDep) -> AttachmentStorage:
    return AttachmentStorage(settings.storage.data_dir)


def get_sync_requester(request: Request) -> SyncRequester:
    """Queues a sync job; ``False`` if one is already waiting."""
    queue: JobQueue = request.app.state.job_queue

    async def request_mailbox_sync(mailbox_id: uuid.UUID) -> bool:
        await queue.ensure_open()
        return await request_sync(mailbox_id)

    return request_mailbox_sync


DbDep = Annotated[AsyncSession, Depends(get_db)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_provider_registry)]
StorageDep = Annotated[AttachmentStorage, Depends(get_attachment_storage)]
SyncRequesterDep = Annotated[SyncRequester, Depends(get_sync_requester)]

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such mailbox"}}
CONNECTION_FAILED: dict[int | str, dict[str, Any]] = {
    422: {"description": "Invalid request, unavailable mailbox type or failed connection test"}
}


async def _mailbox(
    db: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID, permission: MailboxPermission
) -> Mailbox:
    mailbox = await access.get_mailbox(db, user_id, mailbox_id, permission)
    if mailbox is None:
        raise ProblemError(404, detail="Mailbox not found.")
    return mailbox


def _require_type(providers: ProviderRegistry, type: MailboxType) -> None:
    if not providers.is_registered(type):
        raise ProblemError(
            422, detail="This mailbox type is not available.", error_code="type_unavailable"
        )


def _connection_failed(result: ConnectionTestResult) -> ProblemError:
    return ProblemError(422, detail="The connection test failed.", error_code=result.error)


async def _request_sync(requester: SyncRequester, mailbox_id: uuid.UUID) -> None:
    """Queue a sync after a change; the watcher's polling catches up if this fails."""
    try:
        await requester(mailbox_id)
    except Exception as exc:
        log.warning(
            "mail_sync_request_failed", mailbox_id=str(mailbox_id), error_type=type(exc).__name__
        )


async def _read(db: AsyncSession, mailbox: Mailbox) -> MailboxRead:
    await db.refresh(mailbox)
    return (await service.mailbox_reads(db, [mailbox]))[0]


@router.post("/autodiscover")
async def autodiscover(
    body: AutodiscoverRequest, _: CurrentSessionDep, providers: RegistryDep
) -> AutodiscoverResult:
    """Connection suggestions for an address (known providers, else guesses from the
    domain). Offline; nothing is looked up or stored. The address travels in the body so
    it never appears in access logs."""
    suggestions = autodiscovery.suggest(body.address, providers.is_registered)
    return AutodiscoverResult(
        suggestions=[
            AutodiscoverSuggestion(
                type=s.type,
                provider_settings=s.provider_settings,
                source=s.source,
                hints=list(s.hints),
            )
            for s in suggestions
        ]
    )


@router.post("/test", responses=CONNECTION_FAILED)
async def test_mailbox_connection(
    body: MailboxConnection, _: CurrentSessionDep, providers: RegistryDep
) -> ConnectionTestResult:
    """Connect and list the folders without saving anything. A failed test is a normal
    result (``ok: false`` with an error code), not an HTTP error."""
    _require_type(providers, body.type)
    return await service.check_connection(providers.create, service.connection_config(body))


@router.get("")
async def list_mailboxes(current: CurrentSessionDep, db: DbDep) -> list[MailboxRead]:
    """The user's mailboxes with their sync status."""
    mailboxes = list(
        await db.scalars(
            select(Mailbox)
            .where(access.visible_to(current.user_id))
            .order_by(Mailbox.display_name, Mailbox.id)
        )
    )
    return await service.mailbox_reads(db, mailboxes)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={**CONNECTION_FAILED, 409: {"description": "Mailbox already added"}},
)
async def create_mailbox(
    body: MailboxCreate,
    current: CurrentSessionDep,
    db: DbDep,
    providers: RegistryDep,
    requester: SyncRequesterDep,
) -> MailboxRead:
    """Add a mailbox. The connection is tested first; then the initial import starts
    (unless ``sync_enabled`` is false)."""
    _require_type(providers, body.type)
    if await service.find_duplicate(db, current.user_id, body):
        raise ProblemError(409, detail="This mailbox has already been added.")
    result = await service.check_connection(providers.create, service.connection_config(body))
    if not result.ok:
        raise _connection_failed(result)
    mailbox = service.create_mailbox(db, current.user_id, body)
    await db.flush()
    await service.notify(db, mailbox, "created")
    # TODO(#35): audit.record("mailbox.created", mailbox_id=mailbox.id) once the audit
    # log (PR #65) is on main.
    await db.commit()
    log.info("mail_mailbox_created", mailbox_id=str(mailbox.id), type=mailbox.type.value)
    if mailbox.sync_enabled:
        await _request_sync(requester, mailbox.id)
    return await _read(db, mailbox)


@router.get("/{mailbox_id}", responses=NOT_FOUND)
async def get_mailbox(mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> MailboxRead:
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.READ)
    return (await service.mailbox_reads(db, [mailbox]))[0]


@router.patch("/{mailbox_id}", responses={**NOT_FOUND, **CONNECTION_FAILED})
async def update_mailbox(
    mailbox_id: uuid.UUID,
    body: MailboxUpdate,
    current: CurrentSessionDep,
    db: DbDep,
    providers: RegistryDep,
    requester: SyncRequesterDep,
) -> MailboxRead:
    """Rename, change connection settings or credentials (tested before saving), change
    the import period or excluded folder roles, pause (``sync_enabled: false``) or resume
    syncing."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.MANAGE)
    config = service.updated_config(mailbox, body)
    if config is not None:
        _require_type(providers, mailbox.type)
        result = await service.check_connection(providers.create, config)
        if not result.ok:
            raise _connection_failed(result)
    was_enabled = mailbox.sync_enabled
    service.apply_update(mailbox, body)
    await service.notify(db, mailbox, "updated")
    await db.commit()
    log.info("mail_mailbox_updated", mailbox_id=str(mailbox.id))
    resumed = mailbox.sync_enabled and not was_enabled
    if resumed or (mailbox.sync_enabled and (config is not None or body.sync_settings)):
        await _request_sync(requester, mailbox.id)
    return await _read(db, mailbox)


@router.delete("/{mailbox_id}", responses=NOT_FOUND)
async def delete_mailbox(
    mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, storage: StorageDep
) -> MailboxDeleted:
    """Remove the mailbox and everything derived from it: mails, attachments (including
    the files), threads, folders, sync state, processing results, todos, triage results
    and the search index. Hard delete; returns what was removed as confirmation."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.MANAGE)
    messages, attachments = await service.data_counts(db, mailbox.id)
    await service.notify(db, mailbox, "deleted")
    # TODO(#35): audit.record("mailbox.deleted", mailbox_id=mailbox.id) in this
    # transaction once the audit log (PR #65) is on main.
    # Commits (sending the event), then removes the attachment directory.
    await mail_service.delete_mailbox(db, mailbox.id, storage)
    return MailboxDeleted(mailbox_id=mailbox_id, messages=messages, attachments=attachments)


@router.get("/{mailbox_id}/status", responses=NOT_FOUND)
async def get_mailbox_status(
    mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> MailboxSyncStatus:
    """Sync status only (cheap to poll as a fallback to SSE)."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.READ)
    return (await service.statuses(db, [mailbox]))[mailbox.id]


@router.post(
    "/{mailbox_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    responses={**NOT_FOUND, 409: {"description": "Syncing is paused"}},
)
async def sync_mailbox(
    mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, requester: SyncRequesterDep
) -> SyncRequestResult:
    """Start a sync now. Progress arrives as ``mailbox.sync`` events."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.SYNC)
    if not mailbox.sync_enabled:
        raise ProblemError(409, detail="Syncing is paused for this mailbox.")
    return SyncRequestResult(queued=await requester(mailbox.id))


@router.get("/{mailbox_id}/folders", responses=NOT_FOUND)
async def list_folders(
    mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> list[FolderRead]:
    """Folders known from the last sync, with selection and per-folder sync status.
    Before the first sync, use the folder list of the connection test."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.READ)
    return await service.folder_reads(db, mailbox)


@router.patch("/{mailbox_id}/folders", responses=NOT_FOUND)
async def select_folders(
    mailbox_id: uuid.UUID,
    body: FolderSelectionUpdate,
    current: CurrentSessionDep,
    db: DbDep,
    requester: SyncRequesterDep,
) -> list[FolderRead]:
    """Choose which folders are synced. Deselected folders keep their stored mails;
    newly selected folders are imported with the next sync (requested right away)."""
    mailbox = await _mailbox(db, current.user_id, mailbox_id, MailboxPermission.MANAGE)
    selection = {item.id: item.sync_enabled for item in body.folders}
    unknown = await service.select_folders(db, mailbox, selection)
    if unknown:
        raise ProblemError(422, detail="Unknown folder.", error_code="unknown_folder")
    await service.notify(db, mailbox, "updated")
    await db.commit()
    if mailbox.sync_enabled and any(selection.values()):
        await _request_sync(requester, mailbox.id)
    return await service.folder_reads(db, mailbox)

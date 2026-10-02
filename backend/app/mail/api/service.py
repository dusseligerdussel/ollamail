"""Mailbox management on top of the existing mail modules: connection tests through the
provider registry, sync status from ``SyncState``, removal through
``app.mail.service.delete_mailbox``.

Privacy: logs carry mailbox IDs and error codes only, never addresses, display names,
host names or credentials.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, publish
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.api.schemas import (
    ConnectionTestResult,
    FolderRead,
    MailboxConnection,
    MailboxCreate,
    MailboxRead,
    MailboxSyncStatus,
    MailboxUpdate,
    RemoteFolderRead,
    SyncPhase,
)
from app.mail.models import Attachment, Folder, Mailbox, Message, SyncState
from app.mail.models import message_folders as message_folders_table
from app.mail.providers.base import MailboxConfig, ProviderError
from app.mail.providers.registry import ProviderFactory
from app.mail.schemas import SyncSettings
from app.mail.sync.engine import mailbox_config
from app.worker import resource_lock

log = get_logger(__name__)

SYNC_TASK = "mail.sync_mailbox"


# -- connection test ------------------------------------------------------------------


async def check_connection(factory: ProviderFactory, config: MailboxConfig) -> ConnectionTestResult:
    """Connect, authenticate and list the folders, without storing anything."""
    try:
        provider = factory(config)
        try:
            folders = await provider.list_folders()
        finally:
            await provider.aclose()
    except ProviderError as exc:
        log.info("mail_connection_test_failed", type=config.type.value, error=exc.code)
        return ConnectionTestResult(ok=False, error=exc.code)
    return ConnectionTestResult(
        ok=True,
        folders=[
            RemoteFolderRead(
                remote_id=f.remote_id, name=f.name, kind=f.kind, role=f.role, parent_id=f.parent_id
            )
            for f in folders
        ],
    )


def connection_config(
    body: MailboxConnection, mailbox_id: uuid.UUID | None = None
) -> MailboxConfig:
    return MailboxConfig(
        mailbox_id=mailbox_id,
        type=body.type,
        address=body.address,
        settings=dict(body.provider_settings),
        credentials=dict(body.credentials),
    )


def updated_config(mailbox: Mailbox, body: MailboxUpdate) -> MailboxConfig | None:
    """The connection config after ``body``, or ``None`` if the connection is unchanged."""
    if body.provider_settings is None and body.credentials is None:
        return None
    current = mailbox_config(mailbox)
    return MailboxConfig(
        mailbox_id=mailbox.id,
        type=mailbox.type,
        address=mailbox.address,
        settings=dict(body.provider_settings)
        if body.provider_settings is not None
        else current.settings,
        credentials=dict(body.credentials) if body.credentials is not None else current.credentials,
    )


# -- create / update ------------------------------------------------------------------


async def find_duplicate(session: AsyncSession, user_id: uuid.UUID, body: MailboxCreate) -> bool:
    duplicate = await session.scalar(
        select(Mailbox.id).where(
            Mailbox.owner_user_id == user_id,
            Mailbox.type == body.type,
            func.lower(Mailbox.address) == body.address.lower(),
        )
    )
    return duplicate is not None


def create_mailbox(session: AsyncSession, user_id: uuid.UUID, body: MailboxCreate) -> Mailbox:
    mailbox = Mailbox(
        id=uuid7(),
        type=body.type,
        display_name=body.display_name or body.address,
        address=body.address,
        owner_user_id=user_id,
        is_shared=False,
        provider_settings=dict(body.provider_settings),
        credentials=dict(body.credentials) or None,
        sync_enabled=body.sync_enabled,
        sync_settings=body.sync_settings.model_dump(mode="json"),
    )
    session.add(mailbox)
    return mailbox


def apply_update(mailbox: Mailbox, body: MailboxUpdate) -> None:
    if body.display_name is not None:
        mailbox.display_name = body.display_name
    if body.provider_settings is not None:
        mailbox.provider_settings = dict(body.provider_settings)
    if body.credentials is not None:
        mailbox.credentials = dict(body.credentials) or None
    if body.sync_settings is not None:
        changes = body.sync_settings.model_dump(mode="json", exclude_unset=True)
        # ``null`` resets the import period; for the other fields it means "unchanged".
        changes = {k: v for k, v in changes.items() if v is not None or k == "initial_sync_days"}
        current = SyncSettings.model_validate(mailbox.sync_settings or {})
        merged = SyncSettings.model_validate({**current.model_dump(mode="json"), **changes})
        mailbox.sync_settings = merged.model_dump(mode="json")
    if body.sync_enabled is not None:
        mailbox.sync_enabled = body.sync_enabled


async def notify(session: AsyncSession, mailbox: Mailbox, status: str) -> None:
    """Tell the owner's other sessions (tabs, devices) that the mailbox changed; delivered
    on commit."""
    if mailbox.owner_user_id is not None:
        event = Event(type="mailbox.changed", ids={"mailbox_id": mailbox.id}, status=status)
        await publish(session, mailbox.owner_user_id, event)


# -- status ---------------------------------------------------------------------------


def import_pending(cursor: dict[str, Any] | None) -> bool:
    """Whether a folder's initial import is unfinished: never synced, or the provider keeps
    pending import work under ``"import"`` in its cursor (``SyncCursor``)."""
    return not cursor or "import" in cursor


def _excluded(settings: SyncSettings, folder: Folder) -> tuple[bool, bool]:
    """(excluded by role, excluded at all) - the same rule as ``MailboxSync._excluded``."""
    by_role = folder.role in settings.excluded_roles
    return by_role, by_role or folder.remote_id in settings.excluded_folders


async def _queued(session: AsyncSession, mailbox_ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Mailboxes with a sync job waiting or running."""
    if not mailbox_ids:
        return set()
    locks = {resource_lock("mailbox", mailbox_id): mailbox_id for mailbox_id in mailbox_ids}
    rows = await session.execute(
        text(
            "SELECT DISTINCT lock FROM procrastinate_jobs"
            " WHERE task_name = :task AND status IN ('todo', 'doing') AND lock = ANY(:locks)"
        ),
        {"task": SYNC_TASK, "locks": list(locks)},
    )
    return {locks[lock] for (lock,) in rows.all()}


async def folder_reads(session: AsyncSession, mailbox: Mailbox) -> list[FolderRead]:
    settings = SyncSettings.model_validate(mailbox.sync_settings or {})
    folders = list(
        await session.scalars(
            select(Folder).where(Folder.mailbox_id == mailbox.id).order_by(Folder.name)
        )
    )
    states = {
        state.folder_id: state
        for state in await session.scalars(
            select(SyncState).where(
                SyncState.mailbox_id == mailbox.id, SyncState.folder_id.is_not(None)
            )
        )
    }
    link = message_folders_table
    counts: dict[uuid.UUID, int] = {
        folder_id: count
        for folder_id, count in (
            await session.execute(
                select(link.c.folder_id, func.count())
                .join(Folder, Folder.id == link.c.folder_id)
                .where(Folder.mailbox_id == mailbox.id)
                .group_by(link.c.folder_id)
            )
        ).all()
    }
    result = []
    for folder in folders:
        state = states.get(folder.id)
        by_role, excluded = _excluded(settings, folder)
        result.append(
            FolderRead(
                id=folder.id,
                remote_id=folder.remote_id,
                name=folder.name,
                kind=folder.kind,
                role=folder.role,
                sync_enabled=folder.sync_enabled,
                excluded_by_role=by_role,
                synced=folder.sync_enabled and not excluded,
                last_synced_at=state.last_synced_at if state else None,
                last_error=state.last_error if state else None,
                import_pending=import_pending(state.cursor if state else None),
                message_count=counts.get(folder.id, 0),
            )
        )
    return result


async def statuses(
    session: AsyncSession, mailboxes: Sequence[Mailbox]
) -> dict[uuid.UUID, MailboxSyncStatus]:
    """Sync status of several mailboxes with a fixed number of queries."""
    ids = [mailbox.id for mailbox in mailboxes]
    if not ids:
        return {}
    folders: dict[uuid.UUID, list[Folder]] = defaultdict(list)
    for folder in await session.scalars(select(Folder).where(Folder.mailbox_id.in_(ids))):
        folders[folder.mailbox_id].append(folder)
    states: dict[tuple[uuid.UUID, uuid.UUID | None], SyncState] = {
        (state.mailbox_id, state.folder_id): state
        for state in await session.scalars(select(SyncState).where(SyncState.mailbox_id.in_(ids)))
    }
    counts: dict[uuid.UUID, int] = {
        mailbox_id: count
        for mailbox_id, count in (
            await session.execute(
                select(Message.mailbox_id, func.count())
                .where(Message.mailbox_id.in_(ids))
                .group_by(Message.mailbox_id)
            )
        ).all()
    }
    queued = await _queued(session, ids)

    result = {}
    for mailbox in mailboxes:
        settings = SyncSettings.model_validate(mailbox.sync_settings or {})
        selected = [
            f for f in folders[mailbox.id] if f.sync_enabled and not _excluded(settings, f)[1]
        ]
        folder_states = [states.get((mailbox.id, f.id)) for f in selected]
        imported = sum(1 for s in folder_states if s and not import_pending(s.cursor))
        failed = sum(1 for s in folder_states if s and s.last_error)
        overall = states.get((mailbox.id, None))
        phase: SyncPhase
        if not mailbox.sync_enabled:
            phase = "paused"
        elif overall is not None and overall.last_error:
            phase = "error"
        elif mailbox.id in queued:
            phase = "syncing"
        elif overall is None or overall.last_synced_at is None:
            phase = "pending"
        elif imported < len(selected):
            phase = "importing"
        else:
            phase = "idle"
        result[mailbox.id] = MailboxSyncStatus(
            phase=phase,
            last_synced_at=overall.last_synced_at if overall else None,
            last_error=overall.last_error if overall else None,
            sync_queued=mailbox.id in queued,
            folders_total=len(selected),
            folders_imported=imported,
            folders_failed=failed,
            message_count=counts.get(mailbox.id, 0),
        )
    return result


def mailbox_read(mailbox: Mailbox, status: MailboxSyncStatus) -> MailboxRead:
    return MailboxRead(
        id=mailbox.id,
        type=mailbox.type,
        display_name=mailbox.display_name,
        address=mailbox.address,
        is_shared=mailbox.is_shared,
        provider_settings=dict(mailbox.provider_settings or {}),
        has_credentials=bool(mailbox.credentials),
        sync_enabled=mailbox.sync_enabled,
        sync_settings=SyncSettings.model_validate(mailbox.sync_settings or {}),
        status=status,
        created_at=mailbox.created_at,
        updated_at=mailbox.updated_at,
    )


async def mailbox_reads(session: AsyncSession, mailboxes: Sequence[Mailbox]) -> list[MailboxRead]:
    by_id = await statuses(session, mailboxes)
    return [mailbox_read(mailbox, by_id[mailbox.id]) for mailbox in mailboxes]


# -- folders --------------------------------------------------------------------------


async def select_folders(
    session: AsyncSession, mailbox: Mailbox, selection: dict[uuid.UUID, bool]
) -> set[uuid.UUID]:
    """Set ``Folder.sync_enabled``; returns the IDs that are not folders of the mailbox.

    ``SyncSettings.excluded_folders`` follows the selection, so a folder the server
    recreates (or that reappears after a full resync) keeps its choice."""
    folders = {
        folder.id: folder
        for folder in await session.scalars(
            select(Folder).where(Folder.mailbox_id == mailbox.id, Folder.id.in_(selection))
        )
    }
    unknown = set(selection) - set(folders)
    if unknown:
        return unknown
    settings = SyncSettings.model_validate(mailbox.sync_settings or {})
    excluded = set(settings.excluded_folders)
    for folder_id, enabled in selection.items():
        folder = folders[folder_id]
        folder.sync_enabled = enabled
        if enabled:
            excluded.discard(folder.remote_id)
        else:
            excluded.add(folder.remote_id)
    settings.excluded_folders = sorted(excluded)
    mailbox.sync_settings = settings.model_dump(mode="json")
    return set()


# -- removal --------------------------------------------------------------------------


async def data_counts(session: AsyncSession, mailbox_id: uuid.UUID) -> tuple[int, int]:
    """(messages, attachments) of a mailbox, for the removal confirmation."""
    messages = await session.scalar(
        select(func.count()).select_from(Message).where(Message.mailbox_id == mailbox_id)
    )
    attachments = await session.scalar(
        select(func.count()).select_from(Attachment).where(Attachment.mailbox_id == mailbox_id)
    )
    return messages or 0, attachments or 0

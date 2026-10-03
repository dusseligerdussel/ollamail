"""Shared mailboxes (admin only, #34): connect a team mailbox once, assign it to users and
groups.

Admins manage the connection (credentials are stored encrypted, like every mailbox),
the folder selection and the assignments, but see metadata only: no mails, no folder
contents (docs/PRIVACY.md, Admin ≠ Leser). An admin reads a shared mailbox only when
assigned to it like any other user, which the audit log shows. The mailbox is synced
once, however many people read it.

Every change of the assignments is recorded in the audit log (``mailbox.shared`` /
``mailbox.unshared``, one entry per user or group) and takes effect with the next request
of the affected users (``app.mail.access``).
"""

import uuid
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.dependencies import AdminSessionDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.events import Event, publish
from app.core.logging import get_logger
from app.mail import access, deletion
from app.mail.api import service
from app.mail.api.router import (
    CONNECTION_FAILED,
    DeletionRequesterDep,
    RegistryDep,
    SyncRequesterDep,
    _connection_failed,
    _request_deletion,
    _request_sync,
    _require_type,
)
from app.mail.api.schemas import (
    FolderRead,
    FolderSelectionUpdate,
    GroupAssignment,
    MailboxAssignmentRead,
    MailboxAssignmentsUpdate,
    MailboxDeleted,
    MailboxUpdate,
    SharedMailboxCreate,
    SharedMailboxRead,
    SyncRequestResult,
)
from app.mail.models import AssignmentPermission, Mailbox, MailboxAssignment
from app.users.models import User

log = get_logger(__name__)

router = APIRouter(
    prefix="/admin/shared-mailboxes",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such shared mailbox"}}
INVALID_ASSIGNMENT: dict[int | str, dict[str, Any]] = {
    422: {"description": "Unknown user or invalid group"}
}

# Longest group name the audit log stores as a detail (longer ones: assignment ID only).
_AUDIT_GROUP_LENGTH = 128


async def _shared(db: AsyncSession, mailbox_id: uuid.UUID) -> Mailbox:
    # A mailbox being removed only shows up in the list (status ``deleting``).
    mailbox = await db.scalar(
        select(Mailbox).where(
            Mailbox.id == mailbox_id,
            Mailbox.is_shared,
            Mailbox.deletion_requested_at.is_(None),
        )
    )
    if mailbox is None:
        raise ProblemError(404, detail="Shared mailbox not found.")
    return mailbox


async def _reads(db: AsyncSession, mailboxes: Sequence[Mailbox]) -> list[SharedMailboxRead]:
    ids = [mailbox.id for mailbox in mailboxes]
    statuses = await service.statuses(db, mailboxes)
    assignments: dict[uuid.UUID, list[MailboxAssignmentRead]] = {id_: [] for id_ in ids}
    rows = await db.execute(
        select(MailboxAssignment, User.display_name)
        .outerjoin(User, User.id == MailboxAssignment.user_id)
        .where(MailboxAssignment.mailbox_id.in_(ids))
        .order_by(
            MailboxAssignment.group_name.is_not(None),
            User.display_name,
            func.lower(MailboxAssignment.group_name),
            MailboxAssignment.provider,
        )
    )
    for assignment, display_name in rows:
        assignments[assignment.mailbox_id].append(
            MailboxAssignmentRead(
                id=assignment.id,
                user_id=assignment.user_id,
                user_display_name=display_name,
                group=assignment.group_name,
                provider=assignment.provider,
                permission=assignment.permission,
            )
        )
    readers = {mailbox_id: len(await access.reader_ids(db, mailbox_id)) for mailbox_id in ids}
    return [
        SharedMailboxRead(
            **service.mailbox_read(mailbox, statuses[mailbox.id]).model_dump(),
            assignments=assignments[mailbox.id],
            reader_count=readers.get(mailbox.id, 0),
        )
        for mailbox in mailboxes
    ]


async def _read(db: AsyncSession, mailbox: Mailbox) -> SharedMailboxRead:
    await db.refresh(mailbox)
    return (await _reads(db, [mailbox]))[0]


def _group_key(group: str, provider: str | None) -> tuple[str, str]:
    return group.casefold(), provider or ""


def _assignment_details(assignment: MailboxAssignment) -> dict[str, Any]:
    """Audit details: user ID, or the group name if it is short and holds no address."""
    if assignment.user_id is not None:
        return {"principal": "user", "user_id": assignment.user_id}
    details: dict[str, Any] = {"principal": "group", "assignment_id": assignment.id}
    group = assignment.group_name or ""
    if len(group) <= _AUDIT_GROUP_LENGTH and "@" not in group and "\n" not in group:
        details["group"] = group
    if assignment.provider:
        details["provider"] = assignment.provider
    return details


async def _notify_users(
    db: AsyncSession, mailbox_id: uuid.UUID, user_ids: set[uuid.UUID], status: str
) -> None:
    event = Event(type="mailbox.changed", ids={"mailbox_id": mailbox_id}, status=status)
    for user_id in sorted(user_ids):
        await publish(db, user_id, event)


async def _set_assignments(
    db: AsyncSession,
    mailbox: Mailbox,
    users: Sequence[uuid.UUID],
    groups: Sequence[GroupAssignment],
    actor: audit.Actor,
) -> tuple[int, int]:
    """Replace the assignments; returns (added, removed). Audits each change and tells
    the users who gained or lost access (events are delivered on commit)."""
    wanted_users = set(users)
    if wanted_users:
        known = set(await db.scalars(select(User.id).where(User.id.in_(wanted_users))))
        if known != wanted_users:
            raise ProblemError(422, detail="Unknown user.", error_code="unknown_user")
    wanted_groups: dict[tuple[str, str], GroupAssignment] = {}
    for group in groups:
        wanted_groups.setdefault(_group_key(group.group, group.provider), group)

    before = set(await db.scalars(access.readers(mailbox.id)))
    current = list(
        await db.scalars(
            select(MailboxAssignment).where(MailboxAssignment.mailbox_id == mailbox.id)
        )
    )
    removed: list[MailboxAssignment] = []
    for assignment in current:
        if assignment.user_id is not None:
            if assignment.user_id in wanted_users:
                wanted_users.discard(assignment.user_id)
                continue
        else:
            key = _group_key(assignment.group_name or "", assignment.provider)
            if wanted_groups.pop(key, None) is not None:
                continue
        removed.append(assignment)
    added = [
        MailboxAssignment(mailbox_id=mailbox.id, user_id=user_id)
        for user_id in sorted(wanted_users)
    ] + [
        MailboxAssignment(mailbox_id=mailbox.id, group_name=g.group, provider=g.provider)
        for g in wanted_groups.values()
    ]
    target = audit.Target.of(audit.TargetType.MAILBOX, mailbox.id)
    for assignment in removed:
        await audit.record(
            db, actor, audit.AuditAction.MAILBOX_UNSHARED, target, _assignment_details(assignment)
        )
        await db.delete(assignment)
    for assignment in added:
        assignment.permission = AssignmentPermission.READ
        db.add(assignment)
        await db.flush()
        await audit.record(
            db,
            actor,
            audit.AuditAction.MAILBOX_SHARED,
            target,
            {**_assignment_details(assignment), "permission": assignment.permission.value},
        )
    await db.flush()
    after = set(await db.scalars(access.readers(mailbox.id)))
    await _notify_users(db, mailbox.id, before - after, "revoked")
    await _notify_users(db, mailbox.id, after - before, "assigned")
    return len(added), len(removed)


@router.get("")
async def list_shared_mailboxes(_: AdminSessionDep, db: DbDep) -> list[SharedMailboxRead]:
    """All shared mailboxes with sync status and assignments (no mail contents)."""
    mailboxes = list(
        await db.scalars(
            select(Mailbox).where(Mailbox.is_shared).order_by(Mailbox.display_name, Mailbox.id)
        )
    )
    return await _reads(db, mailboxes)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={
        **CONNECTION_FAILED,
        **INVALID_ASSIGNMENT,
        409: {"description": "Shared mailbox already added"},
    },
)
async def create_shared_mailbox(
    body: SharedMailboxCreate,
    admin: AdminSessionDep,
    db: DbDep,
    providers: RegistryDep,
    requester: SyncRequesterDep,
) -> SharedMailboxRead:
    """Connect a shared mailbox (connection tested first) and assign it. The initial
    import starts right away unless ``sync_enabled`` is false."""
    _require_type(providers, body.type)
    if await service.find_duplicate(db, None, body):
        raise ProblemError(409, detail="This shared mailbox has already been added.")
    result = await service.check_connection(providers.create, service.connection_config(body))
    if not result.ok:
        raise _connection_failed(result)
    mailbox = service.create_mailbox(db, None, body)
    await db.flush()
    actor = audit.Actor.user(admin.user_id)
    await audit.record(
        db,
        actor,
        audit.AuditAction.MAILBOX_CREATED,
        audit.Target.of(audit.TargetType.MAILBOX, mailbox.id),
        {"type": mailbox.type.value, "shared": True},
    )
    await _set_assignments(db, mailbox, body.users, body.groups, actor)
    await db.commit()
    log.info("mail_shared_mailbox_created", mailbox_id=str(mailbox.id), type=mailbox.type.value)
    if mailbox.sync_enabled:
        await _request_sync(requester, mailbox.id)
    return await _read(db, mailbox)


@router.get("/{mailbox_id}", responses=NOT_FOUND)
async def get_shared_mailbox(
    mailbox_id: uuid.UUID, _: AdminSessionDep, db: DbDep
) -> SharedMailboxRead:
    return (await _reads(db, [await _shared(db, mailbox_id)]))[0]


@router.patch("/{mailbox_id}", responses={**NOT_FOUND, **CONNECTION_FAILED})
async def update_shared_mailbox(
    mailbox_id: uuid.UUID,
    body: MailboxUpdate,
    admin: AdminSessionDep,
    db: DbDep,
    providers: RegistryDep,
    requester: SyncRequesterDep,
) -> SharedMailboxRead:
    """Rename, change connection settings or credentials (tested before saving), change
    sync settings, pause or resume syncing."""
    mailbox = await _shared(db, mailbox_id)
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
    log.info(
        "mail_shared_mailbox_updated", mailbox_id=str(mailbox.id), by_user_id=str(admin.user_id)
    )
    resumed = mailbox.sync_enabled and not was_enabled
    if resumed or (mailbox.sync_enabled and (config is not None or body.sync_settings)):
        await _request_sync(requester, mailbox.id)
    return await _read(db, mailbox)


@router.put("/{mailbox_id}/assignments", responses={**NOT_FOUND, **INVALID_ASSIGNMENT})
async def set_shared_mailbox_assignments(
    mailbox_id: uuid.UUID, body: MailboxAssignmentsUpdate, admin: AdminSessionDep, db: DbDep
) -> SharedMailboxRead:
    """Replace who may read the mailbox. Removing a user or group revokes access at once:
    from the next request on, its mails, triage, todos, search hits, answers and digests
    are no longer visible to them."""
    mailbox = await _shared(db, mailbox_id)
    added, removed = await _set_assignments(
        db, mailbox, body.users, body.groups, audit.Actor.user(admin.user_id)
    )
    await db.commit()
    log.info(
        "mail_shared_mailbox_assigned",
        mailbox_id=str(mailbox.id),
        added=added,
        removed=removed,
        by_user_id=str(admin.user_id),
    )
    return await _read(db, mailbox)


@router.delete("/{mailbox_id}", status_code=status.HTTP_202_ACCEPTED, responses=NOT_FOUND)
async def delete_shared_mailbox(
    mailbox_id: uuid.UUID, admin: AdminSessionDep, db: DbDep, remover: DeletionRequesterDep
) -> MailboxDeleted:
    """Remove the shared mailbox with all its data and its assignments, in the background
    (as ``DELETE /mailboxes/{id}``)."""
    mailbox = await _shared(db, mailbox_id)
    messages, attachments = await service.data_counts(db, mailbox.id)
    await deletion.request_deletion(db, mailbox, audit.Actor.user(admin.user_id))
    await db.commit()
    log.info("mail_mailbox_deletion_requested", mailbox_id=str(mailbox_id))
    await _request_deletion(remover, mailbox_id)
    return MailboxDeleted(mailbox_id=mailbox_id, messages=messages, attachments=attachments)


@router.post(
    "/{mailbox_id}/sync",
    status_code=status.HTTP_202_ACCEPTED,
    responses={**NOT_FOUND, 409: {"description": "Syncing is paused"}},
)
async def sync_shared_mailbox(
    mailbox_id: uuid.UUID, _: AdminSessionDep, db: DbDep, requester: SyncRequesterDep
) -> SyncRequestResult:
    mailbox = await _shared(db, mailbox_id)
    if not mailbox.sync_enabled:
        raise ProblemError(409, detail="Syncing is paused for this mailbox.")
    return SyncRequestResult(queued=await requester(mailbox.id))


@router.get("/{mailbox_id}/folders", responses=NOT_FOUND)
async def list_shared_mailbox_folders(
    mailbox_id: uuid.UUID, _: AdminSessionDep, db: DbDep
) -> list[FolderRead]:
    """Folders with selection, sync status and counts (no contents)."""
    return await service.folder_reads(db, await _shared(db, mailbox_id))


@router.patch("/{mailbox_id}/folders", responses=NOT_FOUND)
async def select_shared_mailbox_folders(
    mailbox_id: uuid.UUID,
    body: FolderSelectionUpdate,
    _: AdminSessionDep,
    db: DbDep,
    requester: SyncRequesterDep,
) -> list[FolderRead]:
    mailbox = await _shared(db, mailbox_id)
    selection = {item.id: item.sync_enabled for item in body.folders}
    if await service.select_folders(db, mailbox, selection):
        raise ProblemError(422, detail="Unknown folder.", error_code="unknown_folder")
    await service.notify(db, mailbox, "updated")
    await db.commit()
    if mailbox.sync_enabled and any(selection.values()):
        await _request_sync(requester, mailbox.id)
    return await service.folder_reads(db, mailbox)

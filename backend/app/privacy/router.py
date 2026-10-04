"""Privacy API: own data export and account deletion (``/privacy``), retention settings
and user deletion for admins (``/admin/privacy``)."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, CurrentSessionDep, CurrentUserDep, SettingsDep
from app.auth.reauth import ADMIN_REAUTH_RESPONSES, REAUTH_RESPONSES, RecentAdminDep, RecentAuthDep
from app.auth.sessions import clear_session_cookie
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.jobs import JobQueue
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.privacy import deletion, exports, tasks
from app.privacy.models import ExportStatus, RetentionSettingsRecord
from app.privacy.policy import default_policy, get_record, merge
from app.privacy.schemas import (
    AccountDeletion,
    AccountPrivacyRead,
    DataExportRead,
    RetentionRun,
    RetentionSettingsRead,
    RetentionSettingsUpdate,
    RetentionValues,
    UserDeletionResult,
)
from app.privacy.storage import ExportStorage

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]
ExportEnqueuer = Callable[[uuid.UUID], Awaitable[None]]
UserDeletionRequester = Callable[[uuid.UUID], Awaitable[bool]]

router = APIRouter(
    prefix="/privacy", tags=["privacy"], responses={401: {"description": "Not signed in"}}
)
admin_router = APIRouter(
    prefix="/admin/privacy",
    tags=["privacy"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_EXPORT_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such export"}}


def get_export_enqueuer(request: Request) -> ExportEnqueuer:
    queue: JobQueue = request.app.state.job_queue

    async def enqueue(export_id: uuid.UUID) -> None:
        await queue.ensure_open()
        await tasks.enqueue_export(export_id)

    return enqueue


def get_user_deletion_requester(request: Request) -> UserDeletionRequester:
    """Queues ``privacy.delete_user`` for a user marked for deletion."""
    queue: JobQueue = request.app.state.job_queue

    async def request_user_deletion(user_id: uuid.UUID) -> bool:
        await queue.ensure_open()
        return await tasks.defer_user_deletion(user_id)

    return request_user_deletion


def get_file_stores(settings: SettingsDep) -> deletion.FileStores:
    data_dir = settings.storage.data_dir
    return deletion.FileStores(
        digests=DigestStorage(data_dir),
        exports=ExportStorage(data_dir),
    )


EnqueuerDep = Annotated[ExportEnqueuer, Depends(get_export_enqueuer)]
FileStoresDep = Annotated[deletion.FileStores, Depends(get_file_stores)]
UserDeletionRequesterDep = Annotated[UserDeletionRequester, Depends(get_user_deletion_requester)]


async def finish_in_background(
    requester: UserDeletionRequester, result: deletion.DeletionResult
) -> None:
    """Queue the rest of a user deletion after the commit (``app.privacy.deletion``);
    ``privacy.resume_user_deletions`` catches up if this fails."""
    if result.completed:
        return
    try:
        await requester(result.user_id)
    except Exception as exc:
        log.warning(
            "privacy_user_deletion_request_failed",
            user_id=str(result.user_id),
            error_type=type(exc).__name__,
        )


# -- own data ----------------------------------------------------------------------------


@router.get("/account")
async def get_account_privacy(_: CurrentSessionDep, settings: SettingsDep) -> AccountPrivacyRead:
    """Whether the own account can be deleted here, and how long exports stay available."""
    return AccountPrivacyRead(
        self_delete_enabled=settings.privacy.self_delete_enabled,
        export_expiry_hours=settings.privacy.export_expiry_hours,
    )


@router.get("/exports")
async def list_exports(current: CurrentSessionDep, db: DbDep) -> list[DataExportRead]:
    """The own data exports that have not expired, newest first."""
    items = await exports.list_exports(db, current.user_id)
    return [DataExportRead.model_validate(item) for item in items]


@router.post("/exports", status_code=status.HTTP_202_ACCEPTED, responses=REAUTH_RESPONSES)
async def request_export(
    current: RecentAuthDep, db: DbDep, settings: SettingsDep, enqueue: EnqueuerDep
) -> DataExportRead:
    """Start an export of the own data (ZIP with JSON and digest audio) as a background
    job. While one is in progress, that one is returned. Needs a recent confirmation
    (app/auth/reauth.py)."""
    item, created = await exports.request_export(
        db, current.user_id, expiry_hours=settings.privacy.export_expiry_hours
    )
    await db.commit()
    if created:
        await enqueue(item.id)
        log.info("privacy_export_requested", export_id=str(item.id), user_id=current.user_id)
    return DataExportRead.model_validate(item)


@router.get(
    "/exports/{export_id}/download",
    response_class=FileResponse,
    responses={
        **_EXPORT_NOT_FOUND,
        200: {"content": {"application/zip": {}}, "description": "The ZIP file"},
    },
)
async def download_export(
    export_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> FileResponse:
    """Download a finished export before it expires. Only its owner can."""
    item = await exports.get_export(db, current.user_id, export_id)
    now = datetime.now(UTC)
    if (
        item is None
        or item.status is not ExportStatus.READY
        or item.file_path is None
        or (item.expires_at is not None and item.expires_at <= now)
    ):
        raise ProblemError(404, detail="Export not found or expired.")
    path = ExportStorage(settings.storage.data_dir).resolve(item.file_path)
    if not path.is_file():
        raise ProblemError(404, detail="Export not found or expired.")
    await audit.record(
        db,
        audit.Actor.user(current.user_id),
        audit.AuditAction.DATA_EXPORTED,
        audit.Target.of(audit.TargetType.USER, current.user_id),
        {"export_id": item.id, "stage": "downloaded"},
    )
    await db.commit()
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"ollamail-export-{item.created_at:%Y-%m-%d}.zip",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete(
    "/exports/{export_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_EXPORT_NOT_FOUND
)
async def delete_export(
    export_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> Response:
    """Delete an own export and its file before it expires."""
    item = await exports.get_export(db, current.user_id, export_id)
    if item is None:
        raise ProblemError(404, detail="Export not found or expired.")
    await exports.delete_export(db, ExportStorage(settings.storage.data_dir), item)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        403: {"description": "Self-deletion is disabled, or a confirmation is needed"},
        409: {"description": "Last active administrator"},
        422: {"description": "Confirmation does not match"},
    },
)
async def delete_account(
    request: Request,
    body: AccountDeletion,
    user: CurrentUserDep,
    _: RecentAuthDep,
    db: DbDep,
    settings: SettingsDep,
    stores: FileStoresDep,
    finisher: UserDeletionRequesterDep,
) -> Response:
    """Delete the own account with all data (mailboxes, mails, todos, digests, ...) and
    files. Confirmed by entering the account's e-mail address, after a recent confirmation
    of the account (app/auth/reauth.py, 403 reauth-required). Not reversible. Mailboxes
    are removed in the background (#177); the account is gone with this response."""
    if not settings.privacy.self_delete_enabled:
        raise ProblemError(
            403,
            detail="Accounts are deleted by an administrator.",
            type="urn:ollamail:problem:self-delete-disabled",
        )
    if body.confirm_email.strip().lower() != user.email:
        raise ProblemError(
            422,
            detail="The confirmation does not match the account.",
            type="urn:ollamail:problem:confirmation-mismatch",
        )
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    result = await deletion.delete_user(
        db, user.id, stores, actor=audit.Actor.user(user.id), via="self", access_guard=guard
    )
    if result is not None:
        await finish_in_background(finisher, result)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response, settings.auth)
    return response


# -- admin -------------------------------------------------------------------------------


def _retention_read(
    settings: Settings, record: RetentionSettingsRecord | None
) -> RetentionSettingsRead:
    defaults = default_policy(settings)
    values = merge(defaults, record)
    overridden = (
        [name for name in defaults.as_dict() if getattr(record, name) is not None]
        if record is not None
        else []
    )
    last_run = None
    if record is not None and record.last_run_at is not None:
        last_run = RetentionRun(finished_at=record.last_run_at, **(record.last_run or {}))
    return RetentionSettingsRead(
        values=RetentionValues(**values.as_dict()),
        defaults=RetentionValues(**defaults.as_dict()),
        overridden=overridden,
        initial_sync_days=settings.mail.initial_sync_days,
        last_run=last_run,
    )


@admin_router.get("/retention")
async def get_retention(
    _: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> RetentionSettingsRead:
    """Effective retention periods, their environment defaults and the last run."""
    return _retention_read(settings, await get_record(db))


@admin_router.patch("/retention")
async def update_retention(
    body: RetentionSettingsUpdate, admin: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> RetentionSettingsRead:
    """Change retention periods; ``null`` resets one to the environment default. Applies
    from the next run of the retention jobs."""
    record = await get_record(db)
    if record is None:
        record = RetentionSettingsRecord()
        db.add(record)
    changes = body.model_dump(exclude_unset=True)
    for name, value in changes.items():
        setattr(record, name, value)
    await db.flush()
    values = merge(default_policy(settings), record).as_dict()
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.RETENTION_CHANGED,
        audit.Target.of(audit.TargetType.SETTINGS, "retention"),
        {name: values[name] for name in changes},
    )
    await db.commit()
    log.info("privacy_retention_changed", fields=sorted(changes), by_user_id=admin.user_id)
    return _retention_read(settings, record)


@admin_router.delete(
    "/users/{user_id}",
    responses={
        **ADMIN_REAUTH_RESPONSES,
        404: {"description": "No such user"},
        409: {"description": "Last active administrator"},
    },
)
async def delete_user(
    request: Request,
    user_id: uuid.UUID,
    admin: RecentAdminDep,
    db: DbDep,
    stores: FileStoresDep,
    finisher: UserDeletionRequesterDep,
) -> UserDeletionResult:
    """Delete a user with all their data and files (Art. 17). Not reversible. From this
    response on the user is gone everywhere; their mailboxes are removed in the
    background (#177), then the user row."""
    guard = await AdminAccessGuard.start(db, request.app.state.auth_providers)
    result = await deletion.delete_user(
        db,
        user_id,
        stores,
        actor=audit.Actor.user(admin.user_id),
        via="admin",
        access_guard=guard,
    )
    if result is None:
        raise ProblemError(404, detail="User not found.")
    await finish_in_background(finisher, result)
    return UserDeletionResult(user_id=user_id, deleted=True, mailboxes=result.mailboxes)

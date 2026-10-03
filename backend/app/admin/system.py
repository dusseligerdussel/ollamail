"""System status for admins (``/admin/system``): model state per task with downloads,
getting-started facts, and processing counts per mailbox with "retry failed".

Admins see states and counts only, never mail content (docs/PRIVACY.md, "Admin ≠ Leser").
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    MailboxProcessingRead,
    ModelPullRead,
    ModelPullRequest,
    ModelStatusRead,
    RetryFailedRead,
    SystemOverviewRead,
)
from app.ai.llm.config import ResolvedConfig
from app.ai.llm.gateway import LLMGateway, get_llm
from app.ai.llm.ollama import normalize_model_name
from app.ai.llm.types import LLMTask
from app.ai.settings import pulls, store
from app.ai.settings.models import AIModelPull
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.jobs import JobQueue
from app.core.logging import get_logger
from app.digest import service as digest_service
from app.mail.api.service import statuses
from app.mail.models import Mailbox
from app.processing import service as processing
from app.processing.models import MailboxProcessingSettings
from app.processing.tasks import Priority, requeue_messages
from app.users.models import User

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]
LLMDep = Annotated[LLMGateway, Depends(get_llm)]

router = APIRouter(
    prefix="/admin/system",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)


def _pull_read(pull: AIModelPull | None) -> ModelPullRead | None:
    if pull is None:
        return None
    return ModelPullRead(
        status=pulls.PullStatus(pull.status),
        completed=pull.completed,
        total=pull.total,
        error_code=pull.error_code,
        updated_at=pull.updated_at,
    )


def _pull_key(endpoint: str, provider: str, model: str) -> tuple[str, str]:
    return endpoint, normalize_model_name(model) if provider == "ollama" else model


@router.get("/models")
async def get_model_status(_: AdminSessionDep, db: DbDep, llm: LLMDep) -> list[ModelStatusRead]:
    """Each task's model: installed, missing, endpoint unreachable, or disabled (cloud
    endpoint while cloud LLMs are off). Asks every endpoint for its models."""
    by_key = await pulls.list_pulls(db)
    return [
        ModelStatusRead(
            task=item.task,
            endpoint=item.endpoint,
            provider=item.provider,
            model=item.model,
            state=item.state,
            can_pull=item.provider == "ollama" and item.state == "missing",
            pull=_pull_read(by_key.get(_pull_key(item.endpoint, item.provider, item.model))),
        )
        for item in await llm.model_status()
    ]


@router.post(
    "/models/pull",
    status_code=status.HTTP_202_ACCEPTED,
    responses={422: {"description": "Model not assigned to a task on this Ollama endpoint"}},
)
async def pull_model(
    body: ModelPullRequest,
    request: Request,
    _: AdminSessionDep,
    db: DbDep,
    settings: SettingsDep,
) -> ModelPullRead:
    """Download a model that a task uses onto its Ollama endpoint, as a background job.
    Poll ``GET /admin/system/models`` for the progress."""
    config = ResolvedConfig(settings.llm, await store.load_overrides(db, settings.llm.timeout))
    key = _pull_key(body.endpoint, "ollama", body.model)
    if not any(
        (assignment := config.assignment(task)).endpoint.provider == "ollama"
        and _pull_key(assignment.endpoint.name, "ollama", assignment.model) == key
        for task in LLMTask
    ):
        raise ProblemError(422, detail="The model is not assigned to a task on this endpoint.")
    pull, start = await pulls.start_pull(db, *key)
    await db.commit()
    if start:
        queue: JobQueue = request.app.state.job_queue
        await queue.ensure_open()
        await pulls.queue_pull(pull.id)
    read = _pull_read(pull)
    assert read is not None
    return read


@router.get("/overview")
async def get_system_overview(
    admin: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> SystemOverviewRead:
    """Facts for the getting-started checklist and, per mailbox, sync status and the
    number of pending and failed processing steps."""
    rows = (
        await db.execute(
            select(Mailbox, User.display_name)
            .outerjoin(User, User.id == Mailbox.owner_user_id)
            .order_by(Mailbox.is_shared, User.display_name, Mailbox.display_name, Mailbox.id)
        )
    ).all()
    mailboxes = [mailbox for mailbox, _ in rows]
    sync = await statuses(db, mailboxes)
    counts = await processing.count_steps_by_mailbox(db)
    disabled = set(
        await db.scalars(
            select(MailboxProcessingSettings.mailbox_id).where(
                MailboxProcessingSettings.enabled.is_(False)
            )
        )
    )
    digest = await digest_service.settings_row(db, admin.user_id)
    items = []
    for mailbox, owner_name in rows:
        count = counts.get(mailbox.id, processing.StepCounts())
        items.append(
            MailboxProcessingRead(
                id=mailbox.id,
                type=mailbox.type,
                display_name=mailbox.display_name if mailbox.is_shared else None,
                is_shared=mailbox.is_shared,
                owner_name=owner_name,
                sync_phase=sync[mailbox.id].phase,
                sync_error=sync[mailbox.id].last_error,
                processing_enabled=mailbox.id not in disabled,
                pending=count.pending,
                running=count.running,
                failed=count.failed,
            )
        )
    # Mailboxes that need attention first.
    items.sort(key=lambda m: (-m.failed, -m.pending))
    return SystemOverviewRead(
        mailbox_count=len(mailboxes),
        digest_enabled=bool(digest is not None and digest.enabled),
        digest_scheduler_enabled=settings.digest.enabled,
        public_url_set=bool(settings.auth.public_url),
        mailboxes=items,
    )


_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such mailbox"}}


@router.post("/mailboxes/{mailbox_id}/retry-failed", responses=_NOT_FOUND)
async def retry_failed_processing(
    mailbox_id: uuid.UUID, request: Request, _: AdminSessionDep, db: DbDep
) -> RetryFailedRead:
    """Process the failed steps of a mailbox again, behind new mail (``REPROCESS``)."""
    if await db.scalar(select(Mailbox.id).where(Mailbox.id == mailbox_id)) is None:
        raise ProblemError(404, detail="Mailbox not found.")
    message_ids = await processing.reset_failed_steps(db, mailbox_id)
    await db.commit()
    if message_ids:
        queue: JobQueue = request.app.state.job_queue
        await queue.ensure_open()
        await requeue_messages(message_ids, Priority.REPROCESS)
    log.info("processing_failed_requeued", mailbox_id=str(mailbox_id), count=len(message_ids))
    return RetryFailedRead(queued=len(message_ids))

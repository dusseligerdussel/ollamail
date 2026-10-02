"""Todo export API: connect a target system, pick a list, export automatically or by hand.

Every user has at most one target. Only target types the admin allows
(``OLLAMAIL_TODOS_EXPORT_SINKS``) can be connected; credentials are stored encrypted and
never returned. Connecting, changing and disconnecting are in the audit log.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.auth.redirect_flow import public_origin
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.jobs import JobQueue
from app.todos.export import service, tasks
from app.todos.export.base import (
    SinkAuthError,
    SinkError,
    SinkNotFoundError,
    SinkUnavailableError,
    TaskList,
)
from app.todos.export.models import TodoExportTarget
from app.todos.export.registry import available_sinks, create_sink
from app.todos.export.schemas import (
    ExportConnection,
    ExportCounts,
    ExportSettingsRead,
    ExportTargetRead,
    ExportTargetSave,
    ExportTargetUpdate,
    TaskListRead,
)
from app.todos.models import Todo
from app.todos.schemas import TodoRead
from app.todos.service import get_todo

router = APIRouter(
    prefix="/todo-export",
    tags=["todos"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]
Enqueuer = Callable[[uuid.UUID], Awaitable[None]]
NO_TARGET: dict[int | str, dict[str, Any]] = {404: {"description": "Export not connected"}}
CONNECTION_ERRORS: dict[int | str, dict[str, Any]] = {
    422: {"description": "Target not allowed, invalid URL, credentials rejected, no list"},
    502: {"description": "Target not reachable"},
}


def get_export_enqueuer(request: Request) -> Enqueuer:
    """Queues a sync of a committed target."""
    queue: JobQueue = request.app.state.job_queue

    async def enqueue(target_id: uuid.UUID) -> None:
        await queue.ensure_open()
        await tasks.enqueue_sync(target_id)

    return enqueue


def get_sink_builder() -> service.SinkBuilder:
    return create_sink


EnqueuerDep = Annotated[Enqueuer, Depends(get_export_enqueuer)]
SinkBuilderDep = Annotated[service.SinkBuilder, Depends(get_sink_builder)]


async def sync_soon(
    db: AsyncSession, settings: Settings, enqueue: Enqueuer, user_ids: list[uuid.UUID | None]
) -> None:
    """After a committed change to todos: sync the targets of the given users, if any."""
    allowed = available_sinks(settings.todos)
    if not allowed:
        return
    for user_id in dict.fromkeys(user_ids):
        if user_id is None:
            continue
        target = await service.get_target(db, user_id)
        if target is not None and target.sink in allowed:
            await enqueue(target.id)


def _sink_problem(exc: SinkError) -> ProblemError:
    if isinstance(exc, SinkUnavailableError):
        return ProblemError(502, detail="The server did not answer.", error_code=exc.code)
    if isinstance(exc, SinkAuthError):
        return ProblemError(422, detail="The server rejected the credentials.", error_code=exc.code)
    if isinstance(exc, SinkNotFoundError):
        return ProblemError(422, detail="No calendar found at this URL.", error_code=exc.code)
    if exc.code in {"invalid_url", "insecure_url"}:
        return ProblemError(422, detail="Invalid server URL.", error_code=exc.code)
    return ProblemError(502, detail="Unexpected answer from the server.", error_code=exc.code)


def _connection_config(
    body: ExportConnection, settings: Settings, current: TodoExportTarget | None
) -> dict[str, Any]:
    if body.sink not in available_sinks(settings.todos):
        raise ProblemError(
            422, detail="This export target is not enabled.", error_code="sink_not_available"
        )
    password = body.password
    if password is None:
        same = (
            current is not None
            and current.sink == body.sink
            and current.config.get("url") == body.url
            and current.config.get("username") == body.username
        )
        password = str(current.config.get("password", "")) if same and current else ""
    return {"url": body.url, "username": body.username, "password": password}


async def _discover(
    sink_builder: service.SinkBuilder, kind: str, config: dict[str, Any], settings: Settings
) -> list[TaskList]:
    try:
        sink = sink_builder(kind, config, settings.todos)
    except SinkError as exc:
        raise _sink_problem(exc) from None
    try:
        return await sink.list_task_lists()
    except SinkError as exc:
        raise _sink_problem(exc) from None
    finally:
        await sink.aclose()


async def _counts(db: AsyncSession, target: TodoExportTarget) -> ExportCounts:
    state = Todo.external_refs[target.sink]["state"].astext
    rows = await db.execute(
        select(state, func.count())
        .where(
            Todo.external_refs[target.sink]["target"].astext == str(target.id),
            Todo.external_refs[target.sink]["list"].astext == target.list_id,
        )
        .group_by(state)
    )
    counts = {str(name): int(count) for name, count in rows.all() if name}
    return ExportCounts.model_validate(counts)


async def _read(
    db: AsyncSession, settings: Settings, target: TodoExportTarget | None
) -> ExportSettingsRead:
    allowed = available_sinks(settings.todos)
    if target is None:
        return ExportSettingsRead(available_sinks=allowed, target=None)
    return ExportSettingsRead(
        available_sinks=allowed,
        target=ExportTargetRead(
            sink=target.sink,
            url=str(target.config.get("url", "")),
            username=str(target.config.get("username", "")),
            has_password=bool(target.config.get("password")),
            list_id=target.list_id,
            list_name=target.list_name,
            mode=target.mode,
            active=target.sink in allowed,
            last_sync_at=target.last_sync_at,
            last_error=target.last_error,
            counts=await _counts(db, target),
            created_at=target.created_at,
        ),
    )


async def _audit(
    db: AsyncSession, user_id: uuid.UUID, target: TodoExportTarget, change: str
) -> None:
    await audit.record(
        db,
        audit.Actor.user(user_id),
        audit.AuditAction.TODO_EXPORT_CHANGED,
        audit.Target.of(audit.TargetType.SETTINGS, target.id),
        {"sink": target.sink, "change": change, "mode": target.mode.value},
    )


@router.get("")
async def get_export_settings(
    current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> ExportSettingsRead:
    """Own export settings and the target types the admin allows."""
    return await _read(db, settings, await service.get_target(db, current.user_id))


@router.post("/lists", responses=CONNECTION_ERRORS)
async def list_task_lists(
    body: ExportConnection,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    sink_builder: SinkBuilderDep,
) -> list[TaskListRead]:
    """Connect to the target with the given credentials and list the lists that can hold
    todos (CalDAV: calendars with tasks). Nothing is stored."""
    config = _connection_config(body, settings, await service.get_target(db, current.user_id))
    lists = await _discover(sink_builder, body.sink, config, settings)
    return [TaskListRead(id=item.id, name=item.name) for item in lists]


@router.put("", responses=CONNECTION_ERRORS)
async def save_export_settings(
    body: ExportTargetSave,
    request: Request,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    sink_builder: SinkBuilderDep,
    enqueue: EnqueuerDep,
) -> ExportSettingsRead:
    """Connect the export (or change server, account, list or mode). The connection is
    checked and the list must be one of those the server offers. Open todos are exported
    right away in mode ``auto``."""
    target = await service.get_target(db, current.user_id)
    config = _connection_config(body, settings, target)
    lists = await _discover(sink_builder, body.sink, config, settings)
    chosen = next((item for item in lists if item.id == body.list_id), None)
    if chosen is None:
        raise ProblemError(422, detail="Unknown list.", error_code="unknown_list")
    if target is not None and (
        target.sink != body.sink
        or target.config.get("url") != config["url"]
        or target.config.get("username") != config["username"]
    ):
        # Another server or account: the old references mean nothing there.
        await service.forget_refs(db, target)
        await db.delete(target)
        await db.flush()
        target = None
    change = "updated" if target is not None else "connected"
    if target is None:
        target = TodoExportTarget(user_id=current.user_id, sink=body.sink)
        db.add(target)
    target.config = config
    target.list_id = chosen.id
    target.list_name = chosen.name
    target.mode = body.mode
    target.app_url = public_origin(settings, request)
    target.last_error = None
    target.next_poll_at = None
    await db.flush()
    await _audit(db, current.user_id, target, change)
    await db.commit()
    await enqueue(target.id)
    return await _read(db, settings, target)


async def _target(db: AsyncSession, user_id: uuid.UUID) -> TodoExportTarget:
    target = await service.get_target(db, user_id)
    if target is None:
        raise ProblemError(404, detail="Export not connected.")
    return target


@router.patch("", responses=NO_TARGET)
async def update_export_settings(
    body: ExportTargetUpdate,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    enqueue: EnqueuerDep,
) -> ExportSettingsRead:
    """Switch between automatic and manual export."""
    target = await _target(db, current.user_id)
    if body.mode != target.mode:
        target.mode = body.mode
        await _audit(db, current.user_id, target, "updated")
        await db.commit()
        await enqueue(target.id)
    return await _read(db, settings, target)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, responses=NO_TARGET)
async def disconnect_export(current: CurrentSessionDep, db: DbDep) -> Response:
    """Stop exporting and delete the stored credentials. Tasks already exported stay in
    the target system."""
    target = await _target(db, current.user_id)
    await service.forget_refs(db, target)
    await _audit(db, current.user_id, target, "disconnected")
    await db.delete(target)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED, responses=NO_TARGET)
async def sync_now(current: CurrentSessionDep, db: DbDep, enqueue: EnqueuerDep) -> Response:
    """Sync now, including the status check of exported todos."""
    target = await _target(db, current.user_id)
    target.next_poll_at = None
    await db.commit()
    await enqueue(target.id)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/todos/{todo_id}",
    responses={
        404: {"description": "No such todo or export not connected"},
        422: {"description": "Todo of somebody else (team todo assigned to another person)"},
    },
)
async def export_todo(
    todo_id: uuid.UUID,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    enqueue: EnqueuerDep,
) -> TodoRead:
    """Export one todo (manual mode), or export it again after it was deleted in the
    target system."""
    target = await _target(db, current.user_id)
    if target.sink not in available_sinks(settings.todos):
        raise ProblemError(
            422, detail="This export target is not enabled.", error_code="sink_not_available"
        )
    todo = await get_todo(db, current.user_id, todo_id)
    if todo is None:
        raise ProblemError(404, detail="Todo not found.")
    if current.user_id not in {todo.user_id, todo.assignee_id}:
        raise ProblemError(
            422,
            detail="Only own todos and todos assigned to you can be exported.",
            error_code="not_exportable",
        )
    await service.request_export(db, target, todo)
    await db.commit()
    await db.refresh(todo)
    await enqueue(target.id)
    return TodoRead.model_validate(todo)

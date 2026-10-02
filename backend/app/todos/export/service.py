"""Todo export: per-user target settings and the sync with the target system.

Which todos a sync handles (``candidates``), all of the user's own or assigned todos the
user can still see:

* **new** (mode ``auto``): open todos not yet exported to this target and list;
* **pending**: exported by hand (``request_export``) or waiting for their first export;
* **changed**: exported, then changed in ollamail (``updated_at`` ≠ ``synced_at``);
* on a **status check** (every ``OLLAMAIL_TODOS_EXPORT_POLL_MINUTES``): all exported ones,
  to pick up "done" set in the target system, plus those that failed before.

Conflicts: the target system wins for the status if its task changed after the todo
(last change wins); title, description, due date and priority always come from ollamail.
A task deleted in the target system is not exported again unless the user asks.

Every todo is written in its own short transaction with a condition on ``updated_at``, so
a concurrent change by the user is never overwritten and simply syncs on the next run.
Remote writes are idempotent (fixed ``uid`` per todo), so a lost answer only repeats them.
Nothing here logs titles, descriptions or server answers (docs/PRIVACY.md).
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import TodosSettings
from app.core.events import Event, publish
from app.core.logging import get_logger
from app.todos.export.base import (
    RemoteTask,
    RemoteVersion,
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    TaskData,
    TodoSink,
)
from app.todos.export.models import ExportMode, TodoExportTarget
from app.todos.export.refs import ExportRef, RefState, read_ref, with_ref
from app.todos.export.registry import available_sinks, create_sink
from app.todos.models import Todo, TodoStatus
from app.todos.service import visible_todos

log = get_logger(__name__)

SinkBuilder = Callable[[str, Mapping[str, Any], TodosSettings], TodoSink]
# Event for the UI: export states or statuses changed (``todo`` queries refetch).
EVENT_TYPE = "todo.exported"
_UNCHANGED = object()


# -- settings --------------------------------------------------------------------------------


async def get_target(session: AsyncSession, user_id: uuid.UUID) -> TodoExportTarget | None:
    return await session.scalar(select(TodoExportTarget).where(TodoExportTarget.user_id == user_id))


def _ref_of_target(sink: str, target_id: uuid.UUID) -> ColumnElement[bool]:
    return _ref_field(sink, "target") == str(target_id)


async def forget_refs(session: AsyncSession, target: TodoExportTarget) -> int:
    """Drop the references to ``target`` from all todos (the remote tasks stay)."""
    result = await session.execute(
        update(Todo)
        .where(_ref_of_target(target.sink, target.id))
        .values(external_refs=Todo.external_refs.op("-")(target.sink))
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


# -- which todos ------------------------------------------------------------------------------


def _ref_field(sink: str, name: str) -> ColumnElement[str]:
    field: ColumnElement[str] = Todo.external_refs[sink][name].astext
    return field


def _ours(target: TodoExportTarget) -> ColumnElement[bool]:
    return and_(
        _ref_field(target.sink, "target") == str(target.id),
        _ref_field(target.sink, "list") == target.list_id,
    )


def _changed(target: TodoExportTarget) -> ColumnElement[bool]:
    synced_at = sql_cast(_ref_field(target.sink, "synced_at"), TIMESTAMP(timezone=True))
    state = _ref_field(target.sink, "state")
    return or_(
        state == RefState.PENDING.value,
        and_(state == RefState.SYNCED.value, Todo.updated_at != synced_at),
    )


def _candidate_filter(target: TodoExportTarget, *, poll: bool) -> ColumnElement[bool]:
    ours = _ours(target)
    state = _ref_field(target.sink, "state")
    wanted = [and_(ours, _changed(target))]
    if poll:
        wanted.append(and_(ours, state.in_([RefState.SYNCED.value, RefState.ERROR.value])))
    if target.mode == ExportMode.AUTO:
        # ``IS DISTINCT FROM``: todos without any reference for this sink count as new.
        wanted.append(
            and_(
                Todo.status == TodoStatus.OPEN,
                or_(
                    _ref_field(target.sink, "target").is_distinct_from(str(target.id)),
                    _ref_field(target.sink, "list").is_distinct_from(target.list_id),
                ),
            )
        )
    user_id = target.user_id
    return and_(
        or_(Todo.user_id == user_id, Todo.assignee_id == user_id),
        visible_todos(user_id),
        or_(*wanted),
    )


async def candidates(session: AsyncSession, target: TodoExportTarget, *, poll: bool) -> list[Todo]:
    query = (
        select(Todo)
        .where(_candidate_filter(target, poll=poll))
        .order_by(Todo.created_at, Todo.id)
        .execution_options(populate_existing=True)
    )
    return list(await session.scalars(query))


async def needs_sync(session: AsyncSession, target: TodoExportTarget, *, now: datetime) -> bool:
    """Something to send, a status check or a deletion is due. After a failed run only the
    next status check retries, so a broken target is not contacted every minute."""
    if target.next_poll_at is None or target.next_poll_at <= now:
        return True
    if target.last_error is not None:
        return False
    if target.pending_deletions:
        return True
    return bool(await session.scalar(select(exists().where(_candidate_filter(target, poll=False)))))


# -- sync ----------------------------------------------------------------------------------------


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    # Statuses taken over from the target system.
    pulled: int = 0
    removed: int = 0
    deleted: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None
    codes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.created + self.updated + self.pulled + self.removed + self.failed


def mail_url(base: str | None, todo: Todo) -> str | None:
    if not base or todo.message_id is None:
        return None
    return f"{base.rstrip('/')}/inbox?message={todo.message_id}"


def task_data(todo: Todo, *, base_url: str | None, status: TodoStatus | None = None) -> TaskData:
    status = status or todo.status
    return TaskData(
        uid=str(todo.id),
        title=todo.title,
        description=todo.description,
        due_date=todo.due_date,
        priority=todo.priority,
        status=status,
        completed_at=todo.completed_at if status == TodoStatus.DONE else None,
        modified_at=todo.updated_at,
        url=mail_url(base_url, todo),
    )


class _Sync:
    def __init__(
        self,
        session: AsyncSession,
        target: TodoExportTarget,
        sink: TodoSink,
        base_url: str | None,
    ) -> None:
        self.session = session
        self.target = target
        self.sink = sink
        self.base_url = base_url
        self.result = SyncResult()

    def _ref(self, todo: Todo) -> ExportRef | None:
        ref = read_ref(todo.external_refs, self.target.sink)
        if ref is None or ref.target != str(self.target.id) or ref.list != self.target.list_id:
            return None
        return ref

    def _fresh_ref(self) -> ExportRef:
        return ExportRef(
            target=str(self.target.id), list=self.target.list_id, state=RefState.PENDING
        )

    async def _write(
        self,
        todo: Todo,
        loaded_at: datetime,
        ref: ExportRef,
        *,
        version: RemoteVersion | None = None,
        status: TodoStatus | None = None,
    ) -> bool:
        """Store the reference (and a status from the target), unless the todo changed
        meanwhile. ``updated_at`` and ``synced_at`` get the same time."""
        now = await self.session.scalar(select(func.clock_timestamp()))
        assert now is not None
        if version is not None:
            ref = ref.synced(version.remote_id, version.etag, now)
        values: dict[str, Any] = {
            "external_refs": with_ref(todo.external_refs, self.target.sink, ref),
            "updated_at": now,
        }
        if status is not None and status != todo.status:
            values["status"] = status
            values["completed_at"] = now if status == TodoStatus.DONE else None
            values["done_suggested"] = False
        result = await self.session.execute(
            update(Todo)
            .where(Todo.id == todo.id, Todo.updated_at == loaded_at)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await self.session.commit()
        if not getattr(result, "rowcount", 0):
            # Changed by the user while we talked to the target: next run.
            self.result.skipped += 1
            return False
        return True

    async def todo(self, todo: Todo, remote: object) -> None:
        loaded_at = todo.updated_at
        ref = self._ref(todo)
        try:
            await self._sync(todo, loaded_at, ref, remote)
        except (SinkAuthError, SinkNotFoundError) as exc:
            if isinstance(exc, SinkNotFoundError) and ref is not None and ref.id is not None:
                # The task is gone (not the list): deleted in the target system.
                await self._write(todo, loaded_at, ref.removed())
                self.result.removed += 1
                return
            raise
        except SinkError as exc:
            if exc.transient:
                raise
            await self._write(todo, loaded_at, (ref or self._fresh_ref()).failed(exc.code))
            self.result.failed += 1
            self.result.codes.append(exc.code)

    async def _sync(
        self, todo: Todo, loaded_at: datetime, ref: ExportRef | None, remote: object
    ) -> None:
        list_id = self.target.list_id
        if ref is None or ref.id is None:
            version = await self.sink.push(list_id, task_data(todo, base_url=self.base_url))
            if await self._write(todo, loaded_at, ref or self._fresh_ref(), version=version):
                self.result.created += 1
            return
        synced_at = ref.synced_time()
        local_changed = (
            ref.state in {RefState.PENDING, RefState.ERROR}
            or synced_at is None
            or loaded_at != synced_at
        )
        etag = ref.etag
        status: TodoStatus | None = None
        for attempt in range(2):
            if remote is None:
                if ref.state == RefState.PENDING:
                    # Deleted there, but the user asked for this todo: export it again.
                    version = await self.sink.push(list_id, task_data(todo, base_url=self.base_url))
                    if await self._write(todo, loaded_at, ref, version=version):
                        self.result.created += 1
                    return
                await self._write(todo, loaded_at, ref.removed())
                self.result.removed += 1
                return
            if isinstance(remote, RemoteTask):
                etag = remote.etag
                remote_wins = not local_changed or (
                    remote.modified_at is None or remote.modified_at >= loaded_at
                )
                if remote_wins and remote.status != todo.status:
                    status = remote.status
                if not local_changed:
                    version = RemoteVersion(remote.remote_id, remote.etag)
                    if await self._write(todo, loaded_at, ref, version=version, status=status):
                        self.result.pulled += status is not None
                    return
            if not local_changed:
                return
            data = task_data(todo, base_url=self.base_url, status=status)
            try:
                if data.status == TodoStatus.DONE:
                    version = await self.sink.complete(list_id, ref.id, etag, data)
                else:
                    version = await self.sink.update(list_id, ref.id, etag, data)
            except SinkConflictError:
                if attempt:
                    raise
                # Changed there since our last write: compare once more, then retry.
                remote = (await self.sink.changes(list_id, {ref.id: etag})).get(ref.id, _UNCHANGED)
                if remote is _UNCHANGED:
                    etag = None
                continue
            if await self._write(todo, loaded_at, ref, version=version, status=status):
                self.result.updated += 1
                self.result.pulled += status is not None
            return

    async def deletions(self) -> None:
        """Delete the remote copies of todos deleted in ollamail (best effort)."""
        target = await self.session.scalar(
            select(TodoExportTarget).where(TodoExportTarget.id == self.target.id).with_for_update()
        )
        if target is None or not target.pending_deletions:
            return
        pending = list(target.pending_deletions)
        target.pending_deletions = None
        await self.session.commit()
        for item in pending:
            try:
                await self.sink.delete(item.get("list", ""), item.get("id", ""))
                self.result.deleted += 1
            except SinkError as exc:
                if isinstance(exc, SinkAuthError) or exc.transient:
                    await _requeue_deletions(self.session, self.target.id, [item])
                    raise
                self.result.codes.append(exc.code)


async def _requeue_deletions(
    session: AsyncSession, target_id: uuid.UUID, items: list[dict[str, str]]
) -> None:
    target = await session.scalar(
        select(TodoExportTarget).where(TodoExportTarget.id == target_id).with_for_update()
    )
    if target is not None:
        target.pending_deletions = [*(target.pending_deletions or []), *items]
    await session.commit()


async def queue_deletion(session: AsyncSession, todo: Todo) -> uuid.UUID | None:
    """Before deleting ``todo``: remember its remote copy for the owner's target. Returns
    the target ID if a sync should run. The caller commits."""
    for sink, value in (todo.external_refs or {}).items():
        ref = read_ref({sink: value}, sink)
        if ref is None or ref.id is None or ref.state == RefState.REMOVED:
            continue
        try:
            target_id = uuid.UUID(ref.target)
        except ValueError:
            continue
        target = await session.scalar(
            select(TodoExportTarget).where(TodoExportTarget.id == target_id).with_for_update()
        )
        if target is None or target.sink != sink:
            continue
        target.pending_deletions = [
            *(target.pending_deletions or []),
            {"list": ref.list, "id": ref.id},
        ]
        return target.id
    return None


async def sync_target(
    session: AsyncSession,
    target_id: uuid.UUID,
    *,
    settings: TodosSettings,
    public_url: str | None = None,
    sink_builder: SinkBuilder = create_sink,
    now: datetime | None = None,
    force_poll: bool = False,
) -> SyncResult | None:
    """One sync run for one target; ``None`` if it no longer exists or is not allowed."""
    now = now or datetime.now(UTC)
    target = await session.get(TodoExportTarget, target_id, populate_existing=True)
    if target is None or target.sink not in available_sinks(settings):
        return None
    poll = force_poll or target.next_poll_at is None or target.next_poll_at <= now
    config = dict(target.config)
    sink = sink_builder(target.sink, config, settings)
    sync = _Sync(session, target, sink, public_url or target.app_url)
    result = sync.result
    try:
        await sync.deletions()
        todos = await candidates(session, target, poll=poll)
        remote: dict[str, RemoteTask | None] = {}
        if poll:
            known = {}
            for todo in todos:
                ref = sync._ref(todo)
                if ref is not None and ref.id is not None and ref.state != RefState.REMOVED:
                    known[ref.id] = ref.etag
            remote = await sink.changes(target.list_id, known)
        for todo in todos:
            ref = sync._ref(todo)
            state = remote.get(ref.id, _UNCHANGED) if ref and ref.id else _UNCHANGED
            await sync.todo(todo, state)
    except SinkError as exc:
        # Every write above is committed on its own; there is nothing to roll back.
        result.error = "list_not_found" if isinstance(exc, SinkNotFoundError) else exc.code
    finally:
        await sink.aclose()

    target = await session.get(TodoExportTarget, target_id, populate_existing=True)
    if target is None:
        return result
    updated = sink.updated_config()
    if updated is not None and target.config == config:
        # Rotated tokens or sync state; not if the user reconnected meanwhile.
        target.config = dict(updated)
    target.last_sync_at = now
    target.last_error = result.error
    if poll or result.error:
        target.next_poll_at = now + timedelta(minutes=settings.export_poll_minutes)
    await publish(
        session,
        target.user_id,
        Event(type=EVENT_TYPE, status="failed" if result.error else "done"),
    )
    await session.commit()
    # IDs and counts only.
    log.info(
        "todo_export_synced",
        target_id=str(target_id),
        sink=target.sink,
        created=result.created,
        updated=result.updated,
        pulled=result.pulled,
        removed=result.removed,
        deleted=result.deleted,
        failed=result.failed,
        skipped=result.skipped,
        error_code=result.error,
    )
    return result


async def request_export(session: AsyncSession, target: TodoExportTarget, todo: Todo) -> None:
    """Export ``todo`` with the next sync (manual mode, or again after it was deleted in
    the target system). The caller commits."""
    current = read_ref(todo.external_refs, target.sink)
    if current is not None and current.target == str(target.id) and current.list == target.list_id:
        ref = ExportRef(
            target=current.target,
            list=current.list,
            state=RefState.PENDING,
            id=current.id if current.state != RefState.REMOVED else None,
            etag=current.etag if current.state != RefState.REMOVED else None,
        )
    else:
        ref = ExportRef(target=str(target.id), list=target.list_id, state=RefState.PENDING)
    todo.external_refs = with_ref(todo.external_refs, target.sink, ref)


async def due_targets(
    session: AsyncSession, settings: TodosSettings, *, now: datetime
) -> list[uuid.UUID]:
    """Targets with something to do; few rows (one per user at most)."""
    sinks = available_sinks(settings)
    if not sinks:
        return []
    targets = await session.scalars(
        select(TodoExportTarget).where(TodoExportTarget.sink.in_(sinks))
    )
    return [target.id for target in targets if await needs_sync(session, target, now=now)]

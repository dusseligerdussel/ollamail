"""Sync logic with an in-memory target: what is exported, changes in both directions,
conflicts (last change wins), deletions, errors and the schedule."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import TodosSettings
from app.mail.models import MailboxAssignment
from app.todos.export import service
from app.todos.export.base import (
    RemoteVersion,
    SinkAuthError,
    SinkError,
    SinkUnavailableError,
    TaskData,
)
from app.todos.export.models import ExportMode, TodoExportTarget
from app.todos.export.refs import RefState, read_ref
from app.todos.models import Todo, TodoStatus
from app.users.models import User
from tests.factories import make_user
from tests.todos.conftest import make_mailbox
from tests.todos.export.conftest import EXPORT_SETTINGS, FakeSink, builder_for, make_target

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


@pytest.fixture
async def erika(db_session: AsyncSession) -> User:
    return await make_user(db_session, email="erika@example.org")


async def add_todo(
    session: AsyncSession, user: User | None, title: str = "Send report", **values: object
) -> Todo:
    todo = Todo(user_id=user.id if user else None, title=title, **values)
    session.add(todo)
    await session.flush()
    return todo


async def run(
    session: AsyncSession,
    target: TodoExportTarget,
    sink: FakeSink,
    *,
    poll: bool = False,
    settings: TodosSettings = EXPORT_SETTINGS,
    now: datetime = NOW,
) -> service.SyncResult:
    result = await service.sync_target(
        session,
        target.id,
        settings=settings,
        public_url="https://mail.example.org",
        sink_builder=builder_for(sink),
        now=now,
        force_poll=poll,
    )
    assert result is not None
    return result


async def reload(session: AsyncSession, todo: Todo) -> Todo:
    fresh = await session.scalar(
        select(Todo).where(Todo.id == todo.id).execution_options(populate_existing=True)
    )
    assert fresh is not None
    return fresh


def ref_state(todo: Todo) -> RefState | None:
    ref = read_ref(todo.external_refs, "caldav")
    return ref.state if ref else None


async def edit(session: AsyncSession, todo: Todo, **values: object) -> None:
    """A change by the user (new ``updated_at``)."""
    values.setdefault("updated_at", datetime.now(UTC))
    await session.execute(update(Todo).where(Todo.id == todo.id).values(**values))


async def test_auto_mode_exports_open_own_and_assigned_todos(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    other = await make_user(db_session)
    team = await make_mailbox(db_session, None, "team@example.org")
    db_session.add(MailboxAssignment(mailbox_id=team.id, user_id=erika.id))
    mailbox = await make_mailbox(db_session, erika, "erika@example.org")
    own = await add_todo(db_session, erika, "Own", due_date=date(2026, 10, 9))
    from_mail = await add_todo(db_session, erika, "From mail", mailbox_id=mailbox.id)
    assigned = await add_todo(
        db_session, None, "Assigned", mailbox_id=team.id, assignee_id=erika.id
    )
    unassigned = await add_todo(db_session, None, "Unassigned", mailbox_id=team.id)
    done = await add_todo(db_session, erika, "Done", status=TodoStatus.DONE)
    foreign = await add_todo(db_session, other, "Foreign")
    target = await make_target(db_session, erika)

    result = await run(db_session, target, fake_sink)

    assert result.created == 3
    exported = {data.data.title for data in fake_sink.tasks.values()}
    assert exported == {"Own", "From mail", "Assigned"}
    for todo in (own, from_mail, assigned):
        todo = await reload(db_session, todo)
        ref = read_ref(todo.external_refs, "caldav")
        assert ref is not None and ref.state == RefState.SYNCED
        assert (ref.target, ref.list) == (str(target.id), "list-1")
        assert ref.synced_time() == todo.updated_at
        assert todo.export_state is not None and todo.export_state["state"] == "synced"
    for todo in (unassigned, done, foreign):
        assert (await reload(db_session, todo)).external_refs == {}
    assert fake_sink.closed == 1
    target = await reload_target(db_session, target)
    assert target.last_sync_at == NOW and target.last_error is None


async def reload_target(session: AsyncSession, target: TodoExportTarget) -> TodoExportTarget:
    fresh = await session.get(TodoExportTarget, target.id, populate_existing=True)
    assert fresh is not None
    return fresh


async def test_task_data_carries_the_mail_link(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    mailbox = await make_mailbox(db_session, erika, "erika@example.org")
    message_id = uuid.uuid4()
    todo = await add_todo(db_session, erika, mailbox_id=mailbox.id)
    todo.message_id = None
    data = service.task_data(todo, base_url="https://mail.example.org/")
    assert data.url is None
    todo.message_id = message_id
    data = service.task_data(todo, base_url="https://mail.example.org/")
    assert data.url == f"https://mail.example.org/inbox?message={message_id}"
    assert data.uid == str(todo.id)


async def test_nothing_to_do_makes_no_requests(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    fake_sink.calls.clear()

    result = await run(db_session, target, fake_sink, now=NOW + timedelta(minutes=1))

    assert fake_sink.calls == []
    assert result.changed == 0


async def test_local_changes_are_sent(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)

    await edit(db_session, todo, title="Send the final report")
    result = await run(db_session, target, fake_sink)
    assert result.updated == 1
    await edit(db_session, todo, status=TodoStatus.DONE, completed_at=datetime.now(UTC))
    result = await run(db_session, target, fake_sink)

    assert result.updated == 1
    (stored,) = fake_sink.tasks.values()
    assert stored.data.title == "Send the final report"
    assert stored.status == TodoStatus.DONE
    assert ("update", str(todo.id)) in fake_sink.calls


async def test_done_in_the_target_system_is_taken_over(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika, done_suggested=True)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    (remote_id,) = fake_sink.tasks

    fake_sink.edit_remotely(remote_id, TodoStatus.DONE)
    result = await run(db_session, target, fake_sink, poll=True)

    assert result.pulled == 1
    todo = await reload(db_session, todo)
    assert todo.status == TodoStatus.DONE
    assert todo.completed_at is not None
    assert todo.done_suggested is False
    # The pulled status is not sent back.
    fake_sink.calls.clear()
    await run(db_session, target, fake_sink)
    assert [name for name, _ in fake_sink.calls] == []
    # Reopened there as well.
    fake_sink.edit_remotely(remote_id, TodoStatus.OPEN)
    await run(db_session, target, fake_sink, poll=True)
    assert (await reload(db_session, todo)).status == TodoStatus.OPEN


async def test_conflict_the_later_change_wins(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    newer_here = await add_todo(db_session, erika, "Newer here")
    newer_there = await add_todo(db_session, erika, "Newer there")
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    remote = {data.data.uid: remote_id for remote_id, data in fake_sink.tasks.items()}

    # Both changed: one locally after the remote change, one before.
    local_time = datetime.now(UTC)
    fake_sink.edit_remotely(
        remote[str(newer_here.id)], TodoStatus.DONE, local_time - timedelta(minutes=5)
    )
    fake_sink.edit_remotely(
        remote[str(newer_there.id)], TodoStatus.DONE, local_time + timedelta(minutes=5)
    )
    await edit(db_session, newer_here, title="Newer here (edited)", updated_at=local_time)
    await edit(db_session, newer_there, title="Newer there (edited)", updated_at=local_time)

    await run(db_session, target, fake_sink, poll=True)

    here = await reload(db_session, newer_here)
    there = await reload(db_session, newer_there)
    assert here.status == TodoStatus.OPEN
    assert fake_sink.tasks[remote[str(newer_here.id)]].status == TodoStatus.OPEN
    # Status from the target, title from ollamail.
    assert there.status == TodoStatus.DONE
    stored = fake_sink.tasks[remote[str(newer_there.id)]]
    assert (stored.status, stored.data.title) == (TodoStatus.DONE, "Newer there (edited)")


async def test_conflict_without_status_check_retries_once(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    (remote_id,) = fake_sink.tasks
    # Done there a while ago, edited here just now: the update hits a stale ETag.
    fake_sink.edit_remotely(remote_id, TodoStatus.DONE, datetime(2026, 1, 1, tzinfo=UTC))
    await edit(db_session, todo, title="Edited")

    result = await run(db_session, target, fake_sink)

    assert result.updated == 1
    assert fake_sink.tasks[remote_id].status == TodoStatus.OPEN
    assert fake_sink.tasks[remote_id].data.title == "Edited"


async def test_deleted_in_the_target_system_is_not_exported_again(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    fake_sink.tasks.clear()

    result = await run(db_session, target, fake_sink, poll=True)
    assert result.removed == 1
    todo = await reload(db_session, todo)
    assert ref_state(todo) == RefState.REMOVED
    await edit(db_session, todo, title="Edited")
    await run(db_session, target, fake_sink, poll=True)
    assert fake_sink.tasks == {}

    # Unless the user asks for it.
    await service.request_export(db_session, target, await reload(db_session, todo))
    await db_session.flush()
    result = await run(db_session, target, fake_sink)

    assert result.created == 1
    assert ref_state(await reload(db_session, todo)) == RefState.SYNCED


async def test_manual_mode_exports_only_requested_todos(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    chosen = await add_todo(db_session, erika, "Chosen")
    await add_todo(db_session, erika, "Not chosen")
    target = await make_target(db_session, erika, mode=ExportMode.MANUAL)

    assert (await run(db_session, target, fake_sink)).created == 0
    await service.request_export(db_session, target, chosen)
    await db_session.flush()
    assert ref_state(await reload(db_session, chosen)) == RefState.PENDING
    assert await service.needs_sync(db_session, target, now=NOW)
    result = await run(db_session, target, fake_sink)

    assert result.created == 1
    assert [stored.data.title for stored in fake_sink.tasks.values()] == ["Chosen"]
    # A requested todo stays in sync afterwards, done ones included.
    await edit(db_session, chosen, status=TodoStatus.DONE)
    assert (await run(db_session, target, fake_sink)).updated == 1


async def test_another_list_exports_again(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)

    target.list_id = "list-2"
    await db_session.flush()
    await run(db_session, target, fake_sink)

    assert sorted(fake_sink.tasks) == [f"list-1/{todo.id}", f"list-2/{todo.id}"]


async def test_deleted_todos_are_deleted_in_the_target(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)
    (remote_id,) = fake_sink.tasks
    todo = await reload(db_session, todo)

    assert await service.queue_deletion(db_session, todo) == target.id
    await db_session.delete(todo)
    await db_session.flush()
    assert await service.needs_sync(db_session, target, now=NOW)
    result = await run(db_session, target, fake_sink)

    assert result.deleted == 1
    assert fake_sink.deleted == [remote_id]
    assert (await reload_target(db_session, target)).pending_deletions is None


async def test_deletion_of_a_todo_never_exported_needs_no_sync(
    db_session: AsyncSession, erika: User
) -> None:
    todo = await add_todo(db_session, erika)
    await make_target(db_session, erika)

    assert await service.queue_deletion(db_session, todo) is None


async def test_rejected_todos_are_marked_and_retried_on_the_status_check(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    bad = await add_todo(db_session, erika, "Bad")
    good = await add_todo(db_session, erika, "Good")
    target = await make_target(db_session, erika)
    fake_sink.reject[str(bad.id)] = SinkError("http_400")

    result = await run(db_session, target, fake_sink)

    assert (result.created, result.failed) == (1, 1)
    bad = await reload(db_session, bad)
    assert bad.export_state == {
        "sink": "caldav",
        "state": "error",
        "synced_at": None,
        "error": "http_400",
    }
    assert ref_state(await reload(db_session, good)) == RefState.SYNCED
    # Not retried every minute, only with the status check.
    fake_sink.calls.clear()
    await run(db_session, target, fake_sink)
    assert fake_sink.calls == []
    del fake_sink.reject[str(bad.id)]
    await run(db_session, target, fake_sink, poll=True)
    assert ref_state(await reload(db_session, bad)) == RefState.SYNCED


@pytest.mark.parametrize(
    ("error", "code"),
    [(SinkAuthError(), "auth_failed"), (SinkUnavailableError(), "unavailable")],
)
async def test_target_errors_are_stored_on_the_target(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink, error: SinkError, code: str
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    fake_sink.error = error

    result = await run(db_session, target, fake_sink)

    assert result.error == code
    target = await reload_target(db_session, target)
    assert target.last_error == code
    assert target.next_poll_at == NOW + timedelta(minutes=15)
    assert (await reload(db_session, todo)).external_refs == {}
    # Retried with the next status check only.
    assert not await service.needs_sync(db_session, target, now=NOW + timedelta(minutes=1))
    assert await service.needs_sync(db_session, target, now=NOW + timedelta(minutes=15))
    fake_sink.error = None
    await run(db_session, target, fake_sink, now=NOW + timedelta(minutes=15))
    assert (await reload_target(db_session, target)).last_error is None


async def test_a_change_during_the_request_is_not_overwritten(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    push = fake_sink.push

    async def push_while_user_edits(list_id: str, task: TaskData) -> RemoteVersion:
        await edit(db_session, todo, title="Edited meanwhile")
        return await push(list_id, task)

    fake_sink.push = push_while_user_edits  # type: ignore[method-assign]
    result = await run(db_session, target, fake_sink)

    assert (result.created, result.skipped) == (0, 1)
    todo = await reload(db_session, todo)
    assert todo.title == "Edited meanwhile"
    assert todo.external_refs == {}
    # The next run pushes again; the target overwrites the copy of the first attempt.
    fake_sink.push = push  # type: ignore[method-assign]
    await run(db_session, target, fake_sink)
    (stored,) = fake_sink.tasks.values()
    assert stored.data.title == "Edited meanwhile"


async def test_targets_not_allowed_by_the_admin_are_not_synced(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    await add_todo(db_session, erika)
    target = await make_target(db_session, erika)

    result = await service.sync_target(
        db_session, target.id, settings=TodosSettings(), sink_builder=builder_for(fake_sink)
    )

    assert result is None
    assert fake_sink.calls == []
    assert await service.due_targets(db_session, TodosSettings(), now=NOW) == []


async def test_schedule_picks_targets_with_work(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    idle_user = await make_user(db_session)
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    idle = await make_target(db_session, idle_user)

    due = await service.due_targets(db_session, EXPORT_SETTINGS, now=NOW)
    assert {target.id, idle.id} <= set(due)  # first status check
    await run(db_session, target, fake_sink)
    await run(db_session, idle, fake_sink)
    later = NOW + timedelta(minutes=1)
    assert not {target.id, idle.id} & set(
        await service.due_targets(db_session, EXPORT_SETTINGS, now=later)
    )

    await edit(db_session, todo, title="Changed")
    due = await service.due_targets(db_session, EXPORT_SETTINGS, now=later)
    assert target.id in due and idle.id not in due
    assert idle.id in await service.due_targets(
        db_session, EXPORT_SETTINGS, now=NOW + timedelta(minutes=15)
    )


async def test_forget_refs_on_disconnect(
    db_session: AsyncSession, erika: User, fake_sink: FakeSink
) -> None:
    todo = await add_todo(db_session, erika)
    target = await make_target(db_session, erika)
    await run(db_session, target, fake_sink)

    assert await service.forget_refs(db_session, target) == 1
    assert (await reload(db_session, todo)).external_refs == {}

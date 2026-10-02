"""Integration tests: todo API with sign-in and PostgreSQL. Users only reach their own
todos and can only link mails of their own mailboxes."""

import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.todos.models import Todo, TodoStatus
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.todos.conftest import MailData, make_mailbox

pytestmark = pytest.mark.db


@pytest.fixture
async def erika(db_session: AsyncSession, db_client: AsyncClient) -> MailData:
    user = await make_local_user(db_session, "erika@example.org")
    mailbox = await make_mailbox(db_session, user, "erika@example.org")
    assert (await login(db_client, "erika@example.org")).status_code == 200
    return MailData(db_session, user, mailbox)


async def _foreign_todo(session: AsyncSession) -> Todo:
    other: User = await make_local_user(session, "bob@example.org")
    mailbox = await make_mailbox(session, other, "bob@example.org")
    todo = Todo(user_id=other.id, mailbox_id=mailbox.id, title="Bob's secret task")
    session.add(todo)
    await session.flush()
    return todo


async def test_requires_sign_in(db_client: AsyncClient) -> None:
    assert (await db_client.get("/todos")).status_code == 401
    assert (await db_client.post("/todos", json={"title": "x"})).status_code == 401


async def test_create_and_read_manual_todo(db_client: AsyncClient, erika: MailData) -> None:
    response = await db_client.post(
        "/todos",
        json={"title": "  Water the plants ", "due_date": "2026-10-09", "priority": "low"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Water the plants"
    assert body["due_date"] == "2026-10-09"
    assert (body["priority"], body["status"], body["is_manual"]) == ("low", "open", True)
    assert body["message_id"] is None
    assert body["external_refs"] == {}
    fetched = await db_client.get(f"/todos/{body['id']}")
    assert fetched.json() == body


async def test_manual_todo_links_own_mail_only(
    db_client: AsyncClient, erika: MailData, db_session: AsyncSession
) -> None:
    thread = await erika.thread()
    own = await erika.message(thread=thread)
    other_user = await make_local_user(db_session, "bob@example.org")
    other_mailbox = await make_mailbox(db_session, other_user, "bob@example.org")
    foreign = await erika.message(mailbox=other_mailbox)

    linked = await db_client.post("/todos", json={"title": "Reply", "message_id": str(own.id)})
    assert linked.status_code == 201
    assert (linked.json()["mailbox_id"], linked.json()["thread_id"]) == (
        str(erika.mailbox.id),
        str(thread.id),
    )
    rejected = await db_client.post(
        "/todos", json={"title": "Reply", "message_id": str(foreign.id)}
    )
    assert rejected.status_code == 404


async def test_other_users_todos_are_invisible(
    db_client: AsyncClient, erika: MailData, db_session: AsyncSession
) -> None:
    foreign = await _foreign_todo(db_session)

    assert (await db_client.get("/todos")).json() == []
    assert (await db_client.get(f"/todos/{foreign.id}")).status_code == 404
    patch = await db_client.patch(f"/todos/{foreign.id}", json={"status": "done"})
    assert patch.status_code == 404
    assert (await db_client.delete(f"/todos/{foreign.id}")).status_code == 404
    listed = await db_client.get("/todos", params={"mailbox_id": str(foreign.mailbox_id)})
    assert listed.json() == []
    await db_session.refresh(foreign)
    assert foreign.status == TodoStatus.OPEN


async def test_list_filters(db_client: AsyncClient, erika: MailData) -> None:
    other_mailbox = await make_mailbox(erika.session, erika.user, "erika.private@example.org")
    for title, due, status, mailbox in [
        ("a", date(2026, 10, 9), TodoStatus.OPEN, erika.mailbox),
        ("b", date(2026, 10, 20), TodoStatus.OPEN, other_mailbox),
        ("c", None, TodoStatus.OPEN, erika.mailbox),
        ("d", date(2026, 10, 1), TodoStatus.DONE, erika.mailbox),
        ("e", date(2026, 10, 12), TodoStatus.DISMISSED, None),
    ]:
        erika.session.add(
            Todo(
                user_id=erika.user.id,
                mailbox_id=mailbox and mailbox.id,
                title=title,
                due_date=due,
                status=status,
            )
        )
    await erika.session.flush()

    async def titles(**params: object) -> list[str]:
        response = await db_client.get("/todos", params=params)
        assert response.status_code == 200
        return [todo["title"] for todo in response.json()]

    assert await titles() == ["d", "a", "e", "b", "c"]
    assert await titles(status="open") == ["a", "b", "c"]
    assert await titles(status=["done", "dismissed"]) == ["d", "e"]
    assert await titles(mailbox_id=str(other_mailbox.id)) == ["b"]
    assert await titles(status="open", due_before="2026-10-15") == ["a"]
    assert await titles(due_after="2026-10-10") == ["e", "b"]
    assert await titles(limit=2, offset=1) == ["a", "e"]
    assert (await db_client.get("/todos", params={"status": "later"})).status_code == 422


async def test_list_by_message(db_client: AsyncClient, erika: MailData) -> None:
    message = await erika.message()
    other = await erika.message()
    for title, source in [("from mail", message), ("other mail", other), ("manual", None)]:
        erika.session.add(
            Todo(
                user_id=erika.user.id,
                mailbox_id=source and source.mailbox_id,
                message_id=source and source.id,
                title=title,
            )
        )
    await erika.session.flush()

    response = await db_client.get("/todos", params={"message_id": str(message.id)})

    assert response.status_code == 200
    assert [todo["title"] for todo in response.json()] == ["from mail"]


async def test_edit_and_change_status(
    db_client: AsyncClient, erika: MailData, db_session: AsyncSession
) -> None:
    message = await erika.message()
    todo = Todo(
        user_id=erika.user.id,
        mailbox_id=erika.mailbox.id,
        message_id=message.id,
        title="Send report",
        description="For Max",
        due_date=date(2026, 10, 9),
        confidence=0.8,
        done_suggested=True,
    )
    db_session.add(todo)
    await db_session.flush()

    unchanged = await db_client.patch(f"/todos/{todo.id}", json={"title": "Send report"})
    assert unchanged.json()["is_edited"] is False

    edited = await db_client.patch(
        f"/todos/{todo.id}", json={"title": "Send Q3 report", "due_date": None}
    )
    body = edited.json()
    assert (body["title"], body["due_date"], body["description"], body["is_edited"]) == (
        "Send Q3 report",
        None,
        "For Max",
        True,
    )

    done = (await db_client.patch(f"/todos/{todo.id}", json={"status": "done"})).json()
    assert (done["status"], done["done_suggested"]) == ("done", False)
    assert done["completed_at"] is not None
    reopened = (await db_client.patch(f"/todos/{todo.id}", json={"status": "open"})).json()
    assert (reopened["status"], reopened["completed_at"]) == ("open", None)

    invalid = await db_client.patch(f"/todos/{todo.id}", json={"title": "  "})
    assert invalid.status_code == 422


async def test_dismiss_done_suggestion(
    db_client: AsyncClient, erika: MailData, db_session: AsyncSession
) -> None:
    todo = Todo(user_id=erika.user.id, title="Pay invoice", done_suggested=True)
    db_session.add(todo)
    await db_session.flush()

    body = (await db_client.patch(f"/todos/{todo.id}", json={"done_suggested": False})).json()

    assert (body["status"], body["done_suggested"], body["is_edited"]) == ("open", False, False)


async def test_delete(db_client: AsyncClient, erika: MailData) -> None:
    created = (await db_client.post("/todos", json={"title": "Temporary"})).json()

    assert (await db_client.delete(f"/todos/{created['id']}")).status_code == 204
    assert (await db_client.get(f"/todos/{created['id']}")).status_code == 404
    assert (await db_client.delete(f"/todos/{uuid.uuid4()}")).status_code == 404

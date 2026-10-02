"""Todo queries and changes for the API. Every function is scoped to one user: a todo of
another user behaves exactly like a missing one (docs/PRIVACY.md). Team todos of shared
mailboxes are visible to everybody who may read the mailbox (``app.mail.access``), and
only as long as they may."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.access import accessible_mailbox_ids, visible_to
from app.mail.models import Mailbox, Message
from app.todos.models import Todo, TodoStatus
from app.todos.schemas import TodoCreate, TodoUpdate


@dataclass(frozen=True)
class TodoFilter:
    status: Sequence[TodoStatus] = ()
    mailbox_id: uuid.UUID | None = None
    # Inclusive bounds on the due date; todos without one are left out if either is set.
    due_before: date | None = None
    due_after: date | None = None


def visible_todos(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Own todos (without mailbox or from a readable mailbox) and team todos of readable
    shared mailboxes."""
    readable = Todo.mailbox_id.in_(accessible_mailbox_ids(user_id))
    return or_(
        and_(Todo.user_id == user_id, or_(Todo.mailbox_id.is_(None), readable)),
        and_(Todo.user_id.is_(None), readable),
    )


async def list_todos(
    session: AsyncSession,
    user_id: uuid.UUID,
    filters: TodoFilter,
    *,
    limit: int,
    offset: int = 0,
) -> list[Todo]:
    """Todos of the user: earliest due date first, todos without one last, newest first."""
    query = select(Todo).where(visible_todos(user_id))
    if filters.status:
        query = query.where(Todo.status.in_(filters.status))
    if filters.mailbox_id is not None:
        query = query.where(Todo.mailbox_id == filters.mailbox_id)
    if filters.due_before is not None:
        query = query.where(Todo.due_date <= filters.due_before)
    if filters.due_after is not None:
        query = query.where(Todo.due_date >= filters.due_after)
    query = query.order_by(Todo.due_date.asc().nulls_last(), Todo.created_at.desc(), Todo.id.desc())
    return list(await session.scalars(query.limit(limit).offset(offset)))


async def get_todo(session: AsyncSession, user_id: uuid.UUID, todo_id: uuid.UUID) -> Todo | None:
    return await session.scalar(select(Todo).where(Todo.id == todo_id, visible_todos(user_id)))


async def readable_message(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[Message, Mailbox] | None:
    """The message and its mailbox if the user may read the mailbox."""
    row = (
        await session.execute(
            select(Message, Mailbox)
            .join(Mailbox, Mailbox.id == Message.mailbox_id)
            .where(Message.id == message_id, visible_to(user_id))
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


def create_todo(
    session: AsyncSession,
    user_id: uuid.UUID,
    data: TodoCreate,
    source: Message | None,
    *,
    shared: bool = False,
) -> Todo:
    """A manual todo. Linked to a mail of a shared mailbox it becomes a team todo,
    assigned to its creator."""
    todo = Todo(
        user_id=None if shared else user_id,
        assignee_id=user_id if shared else None,
        title=data.title,
        description=data.description or None,
        due_date=data.due_date,
        priority=data.priority,
        is_manual=True,
        mailbox_id=source.mailbox_id if source else None,
        message_id=source.id if source else None,
        thread_id=source.thread_id if source else None,
    )
    session.add(todo)
    return todo


def set_status(todo: Todo, status: TodoStatus) -> None:
    if status == todo.status:
        return
    todo.status = status
    todo.completed_at = datetime.now(UTC) if status == TodoStatus.DONE else None
    todo.done_suggested = False


def update_todo(todo: Todo, data: TodoUpdate) -> None:
    """Apply a partial update. Content changes protect the todo from re-extraction."""
    fields = data.model_fields_set
    edited = False
    if data.title is not None and data.title != todo.title:
        todo.title = data.title
        edited = True
    if "description" in fields and (data.description or None) != todo.description:
        todo.description = data.description or None
        edited = True
    if "due_date" in fields and data.due_date != todo.due_date:
        todo.due_date = data.due_date
        edited = True
    if data.priority is not None and data.priority != todo.priority:
        todo.priority = data.priority
        edited = True
    if edited:
        todo.is_edited = True
    if data.done_suggested is not None:
        todo.done_suggested = data.done_suggested
    if data.status is not None:
        set_status(todo, data.status)

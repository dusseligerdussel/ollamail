"""What goes into a digest: the mails of the period, weighted by triage, and todos.

Mails are those *received* in ``[start, end)`` in the selected mailboxes the user can read,
without drafts, sent mail, trash and spam folders. Triage decides the order (action
required and important first) and which mails are only counted: bulk categories
(newsletters, notifications) become one collective sentence, skipped categories (spam)
are left out. Todos: open todos created in the period, and open todos due by tomorrow
(in the user's time zone), overdue ones included.

Functions take an open session and never commit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DigestSettings
from app.mail.api.access import visible_to
from app.mail.models import Folder, FolderRole, Mailbox, Message, message_folders
from app.todos.models import Todo, TodoPriority, TodoStatus
from app.triage.categories import slugify
from app.triage.models import TriageCategory, TriageResult

EXCLUDED_ROLES = (FolderRole.SENT, FolderRole.DRAFTS, FolderRole.TRASH, FolderRole.JUNK)

# Order of the built-in categories in a digest; other categories rank before ``info``.
CATEGORY_RANK = {"action_required": 0, "important": 1, "waiting_for": 2, "info": 4}
OTHER_RANK = 3


@dataclass(frozen=True, slots=True)
class MailItem:
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    sender: str
    subject: str
    body: str
    received_at: datetime
    category: str | None
    priority: int | None

    @property
    def rank(self) -> tuple[int, int, float]:
        category = CATEGORY_RANK.get(self.category or "", OTHER_RANK)
        return category, self.priority or 2, -self.received_at.timestamp()

    @property
    def is_important(self) -> bool:
        return self.category in {"action_required", "important"} or self.priority == 1


@dataclass(frozen=True, slots=True)
class TodoItem:
    title: str
    due_date: date | None
    priority: TodoPriority


@dataclass(slots=True)
class DigestContent:
    # Mails to summarise, most important first.
    mails: list[MailItem] = field(default_factory=list)
    # Further mails beyond ``max_messages`` (counted only).
    more_mails: int = 0
    # Bulk category key -> senders, in order of arrival.
    bulk: dict[str, list[str]] = field(default_factory=dict)
    new_todos: list[TodoItem] = field(default_factory=list)
    due_todos: list[TodoItem] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.mails) + self.more_mails + sum(len(s) for s in self.bulk.values())

    @property
    def todo_count(self) -> int:
        return len(self.new_todos) + len(self.due_todos)

    @property
    def is_empty(self) -> bool:
        return self.message_count == 0 and self.todo_count == 0


async def selected_mailboxes(
    session: AsyncSession, user_id: uuid.UUID, mailbox_ids: Sequence[uuid.UUID] | None
) -> list[uuid.UUID]:
    """The user's readable mailboxes, limited to ``mailbox_ids`` if given."""
    statement = select(Mailbox.id).where(visible_to(user_id)).order_by(Mailbox.id)
    if mailbox_ids is not None:
        statement = statement.where(Mailbox.id.in_(list(mailbox_ids)))
    return list(await session.scalars(statement))


def _sender(message: Message) -> str:
    sender = message.sender or {}
    return str(sender.get("name") or sender.get("address") or "")


def _received() -> ColumnElement[datetime]:
    return func.coalesce(Message.received_at, Message.sent_at, Message.created_at)


async def collect(
    session: AsyncSession,
    user_id: uuid.UUID,
    mailbox_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    *,
    today: date,
    settings: DigestSettings,
) -> DigestContent:
    """Gather the content of a digest; ``today`` is the local date of the user."""
    content = DigestContent()
    if mailbox_ids:
        await _collect_mails(session, content, mailbox_ids, start, end, settings)
    await _collect_todos(session, content, user_id, mailbox_ids, start, end, today)
    return content


async def _collect_mails(
    session: AsyncSession,
    content: DigestContent,
    mailbox_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    settings: DigestSettings,
) -> None:
    excluded = (
        select(message_folders.c.message_id)
        .join(Folder, Folder.id == message_folders.c.folder_id)
        .where(message_folders.c.message_id == Message.id, Folder.role.in_(EXCLUDED_ROLES))
        .exists()
    )
    received = _received()
    rows = await session.execute(
        select(Message, received, TriageResult.priority, TriageCategory)
        .outerjoin(TriageResult, TriageResult.message_id == Message.id)
        .outerjoin(TriageCategory, TriageCategory.id == TriageResult.category_id)
        .where(
            Message.mailbox_id.in_(list(mailbox_ids)),
            received >= start,
            received < end,
            ~excluded,
        )
        .order_by(received, Message.id)
    )
    bulk = {key.lower() for key in settings.bulk_categories}
    skipped = {key.lower() for key in settings.skip_categories}
    mails: list[MailItem] = []
    for message, received_at, priority, category in rows:
        key = (category.builtin_key or slugify(category.name)) if category is not None else None
        if key in skipped:
            continue
        if key in bulk:
            content.bulk.setdefault(key, []).append(_sender(message))
            continue
        mails.append(
            MailItem(
                message_id=message.id,
                mailbox_id=message.mailbox_id,
                sender=_sender(message),
                subject=message.subject,
                body=(message.body_main or message.body_text)[: settings.map_body_chars],
                received_at=received_at,
                category=key,
                priority=priority,
            )
        )
    mails.sort(key=lambda mail: mail.rank)
    content.mails = mails[: settings.max_messages]
    content.more_mails = len(mails) - len(content.mails)


async def _collect_todos(
    session: AsyncSession,
    content: DigestContent,
    user_id: uuid.UUID,
    mailbox_ids: Sequence[uuid.UUID],
    start: datetime,
    end: datetime,
    today: date,
) -> None:
    # Manual todos and todos from the selected mailboxes.
    source: ColumnElement[bool] = or_(
        Todo.mailbox_id.is_(None), Todo.mailbox_id.in_(list(mailbox_ids))
    )
    is_new = and_(Todo.created_at >= start, Todo.created_at < end)
    is_due = Todo.due_date <= today + timedelta(days=1)
    todos = await session.scalars(
        select(Todo)
        .where(Todo.user_id == user_id, Todo.status == TodoStatus.OPEN, source)
        .where(or_(is_new, is_due))
        .order_by(Todo.due_date.asc().nulls_last(), Todo.created_at, Todo.id)
    )
    for todo in todos:
        item = TodoItem(todo.title, todo.due_date, todo.priority)
        if start <= todo.created_at < end:
            content.new_todos.append(item)
        else:
            content.due_todos.append(item)

"""Todo extraction from one mail (the work of the pipeline step ``todos``).

Flow: skip irrelevant mails (triage category, shared mailbox), drop the untouched todos
an earlier run took from the same mail (idempotency), ask the model with the thread's
open todos as context, then create new todos, update the ones the mail changes and mark
the ones it completes as "done suggested". Nothing is committed here (step contract).

Prompt injection (#170): the mail goes to the model as a data block with a random tag.
A mail with passages addressed to an AI assistant (``app.ai.injection``) yields no todos
at all and is not sent to the model: a todo is the one output that can leave ollamail
without a click (export in mode ``auto``), so instructions in a mail must never make one.
"""

import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import injection
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.types import ChatMessage, LLMTask
from app.ai.prompts.todos import TODOS_EXTRACT
from app.core.config import TodosSettings
from app.mail.models import Mailbox, Message
from app.todos.dates import resolve_due_date
from app.todos.models import Todo, TodoPriority, TodoStatus
from app.users.models import User

# Open todos of a thread shown to the model (oldest first).
MAX_THREAD_TODOS = 20
# Todos taken from one mail at most (also the ``maxItems`` of the answer schema).
MAX_TODOS_PER_MAIL = 5

WEEKDAY_NAMES = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
}
_YES_NO = {"en": ("yes", "no"), "de": ("ja", "nein")}
_NONE = {"en": "(none)", "de": "(keine)"}
_HEADER_LABELS = {"en": ("From", "To", "Subject"), "de": ("Von", "An", "Betreff")}


def _truncate(limit: int) -> Callable[[Any], Any]:
    # Small models overshoot length limits; cut instead of failing the whole answer.
    def cut(value: Any) -> Any:
        if isinstance(value, str):
            value = " ".join(value.split())
            return value[:limit] or None
        return value

    return cut


def _clamp(value: Any) -> Any:
    if isinstance(value, int | float):
        return min(1.0, max(0.0, float(value)))
    return value


def _priority(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip().lower()
        return value if value in {"high", "normal", "low"} else "normal"
    if isinstance(value, int):
        # Triage-style numeric priorities: 1 = high ... 3 = low.
        return {1: "high", 2: "normal", 3: "low"}.get(value, "normal")
    return value


class ExtractedTodo(BaseModel):
    """One task as returned by the model."""

    title: Annotated[str, BeforeValidator(_truncate(255))]
    description: Annotated[str | None, BeforeValidator(_truncate(1000))] = None
    due_phrase: Annotated[str | None, BeforeValidator(_truncate(100))] = None
    due_date: Annotated[str | None, BeforeValidator(_truncate(32))] = None
    priority: Annotated[Literal["high", "normal", "low"], BeforeValidator(_priority)] = "normal"
    confidence: Annotated[float, BeforeValidator(_clamp)] = Field(default=0.5, ge=0, le=1)
    # Number of the listed open todo this one updates.
    updates: int | None = None


def _gate_schema(schema: dict[str, Any]) -> None:
    """JSON schema of the answer: ``asks_user: false`` allows only an empty todo list,
    ``true`` at least one and at most ``MAX_TODOS_PER_MAIL``.

    Endpoints with native structured output turn the schema into a grammar; without the
    bounds small models kept appending todos until the token limit (#158).
    """
    properties = schema.pop("properties")
    required = ["asks_user", "todos", "done"]

    def branch(asks_user: bool) -> dict[str, Any]:
        todos = {**properties["todos"], "minItems": 1, "maxItems": MAX_TODOS_PER_MAIL}
        if not asks_user:
            todos = {**properties["todos"], "maxItems": 0}
        todos.pop("default", None)
        return {
            "type": "object",
            "properties": {
                "asks_user": {"const": asks_user},
                "todos": todos,
                "done": properties["done"],
            },
            "required": required,
        }

    # The root stays ``"type": "object"`` (required by OpenAI-compatible APIs).
    schema.pop("required", None)
    schema["anyOf"] = [branch(False), branch(True)]


class TodoExtraction(BaseModel):
    """Structured output of the ``todos_extract`` prompt."""

    model_config = ConfigDict(json_schema_extra=_gate_schema)

    # The model's yes/no answer before listing tasks; ``False`` discards all new todos.
    # Listed first so the decision precedes the list (models write in schema order).
    asks_user: bool = True
    todos: list[ExtractedTodo] = Field(default_factory=list)
    # Numbers of listed open todos the mail marks as done.
    done: list[int] = Field(default_factory=list)


# Triage category of a message (lower-case key) or ``None`` if unknown.
CategoryLookup = Callable[[AsyncSession, uuid.UUID], Awaitable[str | None]]


async def _no_category(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    return None


_category_lookup: CategoryLookup = _no_category


def set_category_lookup(lookup: CategoryLookup | None) -> None:
    """Install how the triage category of a message is read (``None``: no triage).

    The triage module (#20) provides it; without one every mail is processed.
    """
    global _category_lookup
    _category_lookup = lookup or _no_category


async def message_category(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    category = await _category_lookup(session, message_id)
    return category.strip().lower() if category else None


def _normalize_title(title: str) -> str:
    return re.sub(r"\W+", " ", title.casefold()).strip()


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def reference_date(message: Message, timezone: str) -> date:
    """Day the mail was sent, in the user's time zone."""
    moment = message.sent_at or message.received_at or message.created_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_zone(timezone)).date()


def _address(entry: dict[str, Any] | None) -> str:
    if not entry:
        return ""
    address = str(entry.get("address") or "")
    name = entry.get("name")
    return f"{name} <{address}>" if name else address


def is_outgoing(message: Message, mailbox: Mailbox) -> bool:
    """The user wrote the mail (sender is the mailbox address)."""
    sender = (message.sender or {}).get("address") or ""
    return bool(sender) and str(sender).casefold() == mailbox.address.casefold()


def injected_passages(message: Message) -> int:
    """Passages of subject and text addressed to an AI assistant."""
    body = message.body_main or message.body_text or ""
    return injection.neutralize(message.subject or "").passages + (
        injection.neutralize(body).passages
    )


def build_prompt(
    message: Message,
    mailbox: Mailbox,
    user: User | None,
    reference: date,
    open_todos: Sequence[Todo],
    *,
    tag: str | None = None,
) -> list[ChatMessage]:
    """``user`` is the owner; ``None`` for a shared mailbox (addressed by its name). The
    mail goes into a data block named ``tag`` (random per call unless given)."""
    language = TODOS_EXTRACT.language_for(message.language)
    tag = tag or injection.data_tag()
    yes, no = _YES_NO[language]
    listed = "\n".join(
        f"[{number}] {todo.title}"
        + (f" (due {todo.due_date.isoformat()})" if todo.due_date else "")
        for number, todo in enumerate(open_todos, start=1)
    )
    recipients = ", ".join(_address(entry) for entry in [*message.to, *message.cc])
    subject = injection.neutralize(message.subject or "").text
    body = injection.neutralize(message.body_main or message.body_text or "").text
    labels = _HEADER_LABELS[language]
    mail = (
        f"{labels[0]}: {_address(message.sender)}\n{labels[1]}: {recipients}\n"
        f"{labels[2]}: {subject}\n\n{body}"
    )
    return TODOS_EXTRACT.render(
        message.language,
        user=f"{user.display_name if user else mailbox.display_name} <{mailbox.address}>",
        sent_on=f"{WEEKDAY_NAMES[language][reference.weekday()]}, {reference.isoformat()}",
        outgoing=yes if is_outgoing(message, mailbox) else no,
        open_todos=listed or _NONE[language],
        tag=tag,
        mail=injection.data_block(tag, mail),
    )


async def _thread_todos(
    session: AsyncSession, user_id: uuid.UUID | None, message: Message
) -> list[Todo]:
    """Open todos of the thread: the owner's, or the team todos of a shared mailbox."""
    if message.thread_id is None:
        return []
    owner = (
        Todo.user_id == user_id
        if user_id is not None
        else and_(Todo.user_id.is_(None), Todo.mailbox_id == message.mailbox_id)
    )
    rows = await session.scalars(
        select(Todo)
        .where(
            owner,
            Todo.thread_id == message.thread_id,
            Todo.status == TodoStatus.OPEN,
        )
        .order_by(Todo.created_at, Todo.id)
        .limit(MAX_THREAD_TODOS)
    )
    return list(rows)


async def discard_previous(session: AsyncSession, message_id: uuid.UUID) -> None:
    """Delete todos an earlier run took from this mail, unless the user touched them."""
    await session.execute(
        delete(Todo).where(
            Todo.message_id == message_id,
            Todo.is_manual.is_(False),
            Todo.is_edited.is_(False),
            Todo.status == TodoStatus.OPEN,
            Todo.done_suggested.is_(False),
        )
    )


def _update(todo: Todo, item: ExtractedTodo, due: date | None) -> None:
    todo.confidence = item.confidence
    if todo.is_edited:
        return
    todo.title = item.title
    if item.description:
        todo.description = item.description
    if due is not None:
        todo.due_date = due
    todo.priority = TodoPriority(item.priority)


@dataclass(frozen=True)
class PlannedTodo:
    item: ExtractedTodo
    due: date | None
    # Index of the open thread todo this one updates; ``None`` for a new todo.
    updates: int | None = None


@dataclass(frozen=True)
class ExtractionPlan:
    todos: list[PlannedTodo] = field(default_factory=list)
    # Indexes of open thread todos the mail suggests are done.
    done: list[int] = field(default_factory=list)


def plan_extraction(
    result: TodoExtraction,
    *,
    open_titles: Sequence[str],
    reference: date,
    min_confidence: float,
    outgoing: bool,
    max_todos: int = MAX_TODOS_PER_MAIL,
) -> ExtractionPlan:
    """What to do with the model's answer, without touching the database.

    Drops todos below ``min_confidence``, all new todos of mails the user wrote or the
    model said ask nothing of the user (``asks_user``), keeps the ``max_todos`` most
    confident ones, resolves due dates and matches todos to the open thread todos (by
    the number the model gave, else by equal title), so follow-up mails update instead
    of duplicating.
    """
    done = sorted({n - 1 for n in result.done if 1 <= n <= len(open_titles)})
    if outgoing or not result.asks_user:
        return ExtractionPlan(done=done)
    # Normalised title -> ("open", index) or ("new", position in ``planned``).
    seen: dict[str, tuple[str, int]] = {
        _normalize_title(title): ("open", index) for index, title in enumerate(open_titles)
    }
    planned: list[PlannedTodo] = []
    candidates = [item for item in result.todos if item.confidence >= min_confidence and item.title]
    # Most confident first; ``sorted`` is stable, so ties keep the model's order.
    candidates = sorted(candidates, key=lambda item: -item.confidence)[:max_todos]
    for item in candidates:
        due = resolve_due_date(item.due_phrase, item.due_date, reference)
        key = _normalize_title(item.title)
        if item.updates is not None and 1 <= item.updates <= len(open_titles):
            match: tuple[str, int] | None = ("open", item.updates - 1)
        else:
            match = seen.get(key)
        if match is None:
            seen[key] = ("new", len(planned))
            planned.append(PlannedTodo(item, due))
        elif match[0] == "open":
            seen[key] = match
            planned.append(PlannedTodo(item, due, updates=match[1]))
        # A second new todo with the same title is a duplicate within the answer.
    return ExtractionPlan(todos=planned, done=done)


def apply_plan(
    session: AsyncSession,
    plan: ExtractionPlan,
    *,
    message: Message,
    user_id: uuid.UUID | None,
    open_todos: Sequence[Todo],
) -> list[Todo]:
    """Create, update and flag todos; returns the new ones."""
    for index in plan.done:
        open_todos[index].done_suggested = True
    created: list[Todo] = []
    for planned in plan.todos:
        if planned.updates is not None:
            _update(open_todos[planned.updates], planned.item, planned.due)
            continue
        todo = Todo(
            user_id=user_id,
            mailbox_id=message.mailbox_id,
            message_id=message.id,
            thread_id=message.thread_id,
            title=planned.item.title,
            description=planned.item.description,
            due_date=planned.due,
            priority=TodoPriority(planned.item.priority),
            confidence=planned.item.confidence,
        )
        session.add(todo)
        created.append(todo)
    return created


async def extract_todos(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    llm: LLMGateway,
    settings: TodosSettings,
) -> list[Todo]:
    """Run the extraction for one stored message; returns the newly created todos."""
    message = await session.get(Message, message_id)
    if message is None:
        return []
    mailbox = await session.get(Mailbox, message.mailbox_id)
    if mailbox is None:
        return []
    # Shared mailboxes (no owner) get unassigned team todos; their readers assign them.
    user = None
    if mailbox.owner_user_id is not None:
        user = await session.get(User, mailbox.owner_user_id)
        if user is None:
            return []
    user_id = user.id if user is not None else None

    if not settings.extraction_enabled:
        return []
    await discard_previous(session, message.id)
    await session.flush()
    category = await message_category(session, message.id)
    if category is not None and category in {c.lower() for c in settings.skip_categories}:
        return []
    if not (message.body_main or message.body_text or message.subject).strip():
        return []
    injected = injected_passages(message)
    if injected:
        injection.count("todos", injected)
        return []

    open_todos = await _thread_todos(session, user_id, message)
    reference = reference_date(message, user.timezone if user is not None else "UTC")
    result = await llm.complete_structured(
        LLMTask.TODOS,
        build_prompt(message, mailbox, user, reference, open_todos),
        TodoExtraction,
        prompt_version=TODOS_EXTRACT.id,
        language=message.language,
    )
    plan = plan_extraction(
        result,
        open_titles=[todo.title for todo in open_todos],
        reference=reference,
        min_confidence=settings.min_confidence,
        outgoing=is_outgoing(message, mailbox),
        max_todos=settings.max_per_mail,
    )
    created = apply_plan(session, plan, message=message, user_id=user_id, open_todos=open_todos)
    await session.flush()
    return created

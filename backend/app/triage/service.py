"""Triage of one message (pipeline step) and the database side of the API.

Functions take an open session and never commit.
"""

import base64
import binascii
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import CloudLLMDisabledError, LLMGateway, LLMOutputError, LLMTask
from app.core.config import TriageSettings
from app.core.events import Event
from app.core.ids import uuid7
from app.mail import listing
from app.mail.access import publish_to_readers, visible_to
from app.mail.models import FolderRole, Mailbox, Message
from app.mail.providers.base import Flag
from app.processing.steps import StepError
from app.triage.categories import EffectiveCategory, effective_categories, slugify
from app.triage.classify import classify, mail_view
from app.triage.feedback import select_examples
from app.triage.models import (
    TriageCategory,
    TriageFeedback,
    TriageResult,
    TriageSenderRule,
    TriageSource,
)
from app.triage.prompts import TRIAGE_PROMPT
from app.triage.rules import SenderRule, prefilter
from app.users.models import User

# Published when a message got (or changed) its category; carries IDs only.
TRIAGED_EVENT = "message.triaged"


@dataclass(frozen=True, slots=True)
class Decision:
    category_id: uuid.UUID
    priority: int
    source: TriageSource
    reason: str | None = None
    rule: str | None = None
    model: str | None = None
    prompt_version: str | None = None


async def sender_rules(session: AsyncSession, user_id: uuid.UUID) -> list[SenderRule]:
    rules = await session.scalars(
        select(TriageSenderRule).where(TriageSenderRule.user_id == user_id)
    )
    return [SenderRule(r.sender, r.category_id, r.priority) for r in rules]


async def _decide(
    session: AsyncSession,
    message: Message,
    mailbox: Mailbox,
    categories: list[EffectiveCategory],
    llm: LLMGateway,
    settings: TriageSettings,
) -> Decision:
    owner_id = mailbox.owner_user_id
    sender_address = (message.sender or {}).get("address")
    if settings.prefilter_enabled:
        rules = await sender_rules(session, owner_id) if owner_id is not None else []
        ruled = prefilter(message.headers, sender_address, rules, categories)
        if ruled is not None:
            return Decision(ruled.category_id, ruled.priority, ruled.source, rule=ruled.rule)

    view = mail_view(
        subject=message.subject,
        sender=message.sender,
        to=message.to,
        cc=message.cc,
        mailbox_address=mailbox.address,
        date=message.received_at or message.sent_at,
        body_main=message.body_main,
        body_text=message.body_text,
        body_chars=settings.max_body_chars,
    )
    language = message.language
    # Few-shot examples only from the owner's own corrections, or for a shared mailbox from
    # the corrections made in this mailbox (docs/PRIVACY.md).
    examples = await select_examples(
        session,
        owner_id,
        view,
        categories,
        settings,
        llm=llm,
        exclude_message_id=message.id,
        shared_mailbox_id=None if owner_id is not None else mailbox.id,
    )
    if owner_id is not None:
        user = await session.get(User, owner_id)
        if user is not None:
            # The reason is shown to the owner, so it is written in the UI language.
            language = user.language
    try:
        decision = await classify(llm, view, categories, examples, language=language)
    except CloudLLMDisabledError:
        raise StepError("llm_cloud_disabled", permanent=True) from None
    except LLMOutputError:
        raise StepError("llm_output_invalid") from None
    return Decision(
        decision.category.id,
        decision.priority,
        TriageSource.LLM,
        reason=decision.reason,
        model=await llm.assigned_model(LLMTask.TRIAGE),
        prompt_version=TRIAGE_PROMPT.id,
    )


async def save_result(
    session: AsyncSession, message_id: uuid.UUID, decision: Decision
) -> TriageResult:
    """Insert or replace the result; marks it for write-back if the category changed."""
    result = await session.scalar(
        select(TriageResult).where(TriageResult.message_id == message_id).with_for_update()
    )
    if result is None:
        result = TriageResult(message_id=message_id, write_back_pending=True)
        session.add(result)
    elif result.category_id != decision.category_id:
        result.write_back_pending = True
    result.category_id = decision.category_id
    result.priority = decision.priority
    result.source = decision.source
    result.reason = decision.reason
    result.rule = decision.rule
    result.model = decision.model
    result.prompt_version = decision.prompt_version
    await session.flush()
    return result


async def triage_message(
    session: AsyncSession, message_id: uuid.UUID, *, llm: LLMGateway, settings: TriageSettings
) -> TriageResult | None:
    """Classify one message (idempotent). A correction by the user is never replaced."""
    row = (
        await session.execute(
            select(Message, Mailbox)
            .join(Mailbox, Mailbox.id == Message.mailbox_id)
            .where(Message.id == message_id)
        )
    ).first()
    if row is None:
        return None
    message, mailbox = row
    existing = await session.scalar(
        select(TriageResult).where(TriageResult.message_id == message_id)
    )
    if existing is not None and existing.source == TriageSource.USER:
        return existing
    # Shared mailboxes use the organisation categories (``owner_user_id IS NULL``).
    categories = await effective_categories(session, mailbox.owner_user_id)
    if not categories:
        raise StepError("triage_no_categories", permanent=True)
    decision = await _decide(session, message, mailbox, categories, llm, settings)
    return await save_result(session, message_id, decision)


async def publish_triaged(
    session: AsyncSession, message_id: uuid.UUID, mailbox_id: uuid.UUID
) -> None:
    """Tell the UI of everybody who reads the mailbox (owner or assigned users of a shared
    mailbox) that the category of a message is (newly) known."""
    event = Event(type=TRIAGED_EVENT, ids={"message_id": message_id, "mailbox_id": mailbox_id})
    await publish_to_readers(session, mailbox_id, event)


async def category_key(session: AsyncSession, message_id: uuid.UUID) -> str | None:
    """Stable key of a message's triage category (``newsletter``, slug of an own category's
    name) or ``None`` if not triaged. Used by the todo step to skip bulk mail."""
    category = await session.scalar(
        select(TriageCategory)
        .join(TriageResult, TriageResult.category_id == TriageCategory.id)
        .where(TriageResult.message_id == message_id)
    )
    if category is None:
        return None
    return category.builtin_key or slugify(category.name)


# -- API helpers -----------------------------------------------------------------------


async def readable_message(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[Message, Mailbox] | None:
    """The message and its mailbox if ``user_id`` may read the mailbox."""
    row = (
        await session.execute(
            select(Message, Mailbox)
            .join(Mailbox, Mailbox.id == Message.mailbox_id)
            .where(Message.id == message_id, visible_to(user_id))
        )
    ).first()
    return (row[0], row[1]) if row is not None else None


async def correct(
    session: AsyncSession,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    category_id: uuid.UUID,
    priority: int,
) -> TriageResult:
    """Store the user's correction as result and as few-shot example of this user."""
    result = await save_result(
        session, message_id, Decision(category_id, priority, TriageSource.USER)
    )
    statement = insert(TriageFeedback).values(
        id=uuid7(),
        user_id=user_id,
        message_id=message_id,
        category_id=category_id,
        priority=priority,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[TriageFeedback.user_id, TriageFeedback.message_id],
            set_={
                "category_id": category_id,
                "priority": priority,
                "updated_at": func.now(),
            },
        )
    )
    return result


@dataclass(frozen=True, slots=True)
class InboxEntry:
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    subject: str
    sender_name: str | None
    sender_address: str | None
    received_at: datetime | None
    category_id: uuid.UUID | None
    priority: int | None
    source: TriageSource | None
    reason: str | None


def _bucket(visible: Sequence[uuid.UUID]) -> ColumnElement[uuid.UUID | None]:
    """The visible category of a message, NULL for the uncategorised group."""
    if not visible:
        return literal(None, type_=TriageResult.category_id.type)
    return case((TriageResult.category_id.in_(visible), TriageResult.category_id))


async def _segment_counts(
    session: AsyncSession,
    visible: Sequence[uuid.UUID],
    mailbox_ids: Sequence[uuid.UUID],
    conditions: Sequence[ColumnElement[bool]],
) -> dict[tuple[uuid.UUID | None, int | None], int]:
    """Messages per visible category (``None``: uncategorised) and priority; one query."""
    if not mailbox_ids:
        return {}
    bucket = _bucket(visible).label("bucket")
    rows = await session.execute(
        select(bucket, TriageResult.priority, func.count())
        .select_from(Message)
        .outerjoin(TriageResult, TriageResult.message_id == Message.id)
        .where(Message.mailbox_id.in_(mailbox_ids), *conditions)
        .group_by(bucket, TriageResult.priority)
    )
    return {(row[0], row[1]): int(row[2]) for row in rows}


def _per_category(
    visible: Sequence[uuid.UUID], counts: dict[tuple[uuid.UUID | None, int | None], int]
) -> dict[uuid.UUID | None, int]:
    """Messages per visible category in the order of ``visible``, then ``None``."""
    totals = dict.fromkeys([*visible, None], 0)
    for (category_id, _), count in counts.items():
        totals[category_id] += count
    return totals


async def inbox(
    session: AsyncSession,
    user_id: uuid.UUID,
    visible: Sequence[uuid.UUID],
    *,
    mailbox_id: uuid.UUID | None,
    per_group: int,
) -> tuple[dict[uuid.UUID | None, list[InboxEntry]], dict[uuid.UUID | None, int]]:
    """Messages in the inbox folders of the mailboxes the user may read, grouped by category in the
    order of ``visible`` (``None`` = not triaged yet, or in a hidden or deleted category):
    highest priority first, then newest; plus the total per group. Two queries, whatever the
    number of categories."""
    visible = list(visible)
    mailbox_ids = await listing.readable_mailbox_ids(session, user_id, mailbox_id)
    inbox_folders = await listing.folder_ids(session, mailbox_ids, FolderRole.INBOX)
    conditions = [listing.in_folders(inbox_folders)]
    totals = _per_category(
        visible, await _segment_counts(session, visible, mailbox_ids, conditions)
    )
    groups: dict[uuid.UUID | None, list[InboxEntry]] = {category_id: [] for category_id in totals}
    if not mailbox_ids:
        return groups, totals
    bucket = _bucket(visible)
    ranked = (
        select(
            Message.id.label("message_id"),
            bucket.label("bucket"),
            func.row_number()
            .over(
                partition_by=bucket,
                order_by=(TriageResult.priority.asc().nulls_last(), *listing.NEWEST_FIRST),
            )
            .label("rank"),
        )
        .outerjoin(TriageResult, TriageResult.message_id == Message.id)
        .where(Message.mailbox_id.in_(mailbox_ids), *conditions)
        .subquery()
    )
    rows = await session.execute(
        select(Message, ranked.c.bucket, TriageResult)
        .join(ranked, ranked.c.message_id == Message.id)
        .outerjoin(TriageResult, TriageResult.message_id == Message.id)
        .where(ranked.c.rank <= per_group)
        .order_by(ranked.c.rank)
        .options(*listing.without_bodies())
    )
    for message, category_id, result in rows:
        groups[category_id].append(
            InboxEntry(
                message_id=message.id,
                mailbox_id=message.mailbox_id,
                subject=message.subject,
                sender_name=(message.sender or {}).get("name"),
                sender_address=(message.sender or {}).get("address"),
                received_at=message.received_at or message.sent_at,
                category_id=result.category_id if result is not None else None,
                priority=result.priority if result is not None else None,
                source=result.source if result is not None else None,
                reason=result.reason if result is not None else None,
            )
        )
    return groups, totals


async def results_of(
    session: AsyncSession, user_id: uuid.UUID, message_ids: Sequence[uuid.UUID]
) -> list[TriageResult]:
    """Triage results of those ``message_ids`` that are in mailboxes ``user_id`` may read."""
    if not message_ids:
        return []
    rows = await session.scalars(
        select(TriageResult)
        .join(Message, Message.id == TriageResult.message_id)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .where(TriageResult.message_id.in_(message_ids), visible_to(user_id))
    )
    return list(rows)


# Priorities in list order; ``None`` (not triaged yet) last.
_PRIORITIES: tuple[int | None, ...] = (1, 2, 3, None)


def _segments(visible: Sequence[uuid.UUID]) -> list[tuple[int, int | None]]:
    """The inbox in list order, cut into segments (category position in ``visible``, the
    uncategorised at ``len(visible)``; priority). Only messages without a result have no
    priority, and they are uncategorised."""
    return [
        (position, priority)
        for position in range(len(visible) + 1)
        for priority in _PRIORITIES
        if priority is not None or position == len(visible)
    ]


@dataclass(frozen=True, slots=True)
class InboxCursor:
    """Position after the last row of a page: its segment (see ``_segments``), its place in
    the segment (``sort_date``, ``id``) and the segments that are not empty (bit ``i`` for
    segment ``i``), as counted for the first page."""

    position: int
    priority: int | None
    sort_date: datetime
    message_id: uuid.UUID
    filled: int

    def encode(self) -> str:
        raw = "|".join(
            (
                str(self.position),
                str(self.priority or 0),
                self.sort_date.isoformat(),
                str(self.message_id),
                format(self.filled, "x"),
            )
        )
        return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()

    @classmethod
    def decode(cls, value: str) -> "InboxCursor":
        """Raises ``ValueError`` for a malformed cursor."""
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
            position, priority, sort_date, message_id, filled = raw.split("|")
            return cls(
                int(position),
                int(priority) or None,
                datetime.fromisoformat(sort_date),
                uuid.UUID(message_id),
                int(filled, 16),
            )
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise ValueError("invalid cursor") from None


@dataclass(frozen=True, slots=True)
class InboxPage:
    # Messages with their visible category (``None``: untriaged, hidden or deleted) and
    # priority.
    rows: list[tuple[Message, uuid.UUID | None, int | None]]
    # Pass as ``cursor`` for the next page; ``None`` on the last page.
    next_cursor: InboxCursor | None
    # Messages matching the filter and messages per visible category in the user's order,
    # then ``None`` (without the category filter); only for the first page.
    total: int | None
    counts: dict[uuid.UUID | None, int] | None


def _in_segment(
    visible: Sequence[uuid.UUID], position: int, priority: int | None
) -> ColumnElement[bool]:
    result = select(TriageResult.id).where(TriageResult.message_id == Message.id)
    if priority is None:
        return ~result.exists()
    result = result.where(TriageResult.priority == priority)
    if position < len(visible):
        return result.where(TriageResult.category_id == visible[position]).exists()
    return result.where(
        or_(TriageResult.category_id.is_(None), TriageResult.category_id.not_in(visible))
    ).exists()


async def inbox_page(
    session: AsyncSession,
    user_id: uuid.UUID,
    visible: Sequence[uuid.UUID],
    *,
    mailbox_id: uuid.UUID | None,
    unread: bool | None,
    category: uuid.UUID | Literal["none"] | None,
    cursor: InboxCursor | None,
    limit: int,
) -> InboxPage:
    """Inbox messages of the user's mailboxes ordered by category (user's order, the
    uncategorised last), then priority, then newest first. ``category`` filters to one
    visible category, or ``"none"`` to the uncategorised ones.

    Pages with a keyset: the segments (category x priority) are read one after the other,
    each newest first along ``ix_mail_messages_mailbox_id_sort_date_id``, until the page is
    full. The first page counts the messages per segment (one query); empty segments are
    skipped, as reading one would walk the whole index."""
    visible = list(visible)
    mailbox_ids = await listing.readable_mailbox_ids(session, user_id, mailbox_id)
    inbox_folders = await listing.folder_ids(session, mailbox_ids, FolderRole.INBOX)
    conditions: list[ColumnElement[bool]] = [listing.in_folders(inbox_folders)]
    if unread is True:
        conditions.append(~Message.flags.contains([Flag.SEEN.value]))
    elif unread is False:
        conditions.append(Message.flags.contains([Flag.SEEN.value]))

    segments = _segments(visible)
    total = counts = None
    if cursor is None:
        by_segment = await _segment_counts(session, visible, mailbox_ids, conditions)
        filled = sum(
            1 << index
            for index, (position, priority) in enumerate(segments)
            if by_segment.get((visible[position] if position < len(visible) else None, priority))
        )
        counts = _per_category(visible, by_segment)
        if category is None:
            total = sum(counts.values())
        elif category == "none":
            total = counts[None]
        else:
            total = counts.get(category, 0)
    else:
        filled = cursor.filled

    if category is None:
        positions = range(len(visible) + 1)
    elif category == "none":
        positions = range(len(visible), len(visible) + 1)
    else:
        positions = range(visible.index(category), visible.index(category) + 1)
    start = 0
    if cursor is not None:
        current = (cursor.position, cursor.priority)
        start = segments.index(current) if current in segments else len(segments)

    rows: list[tuple[Message, uuid.UUID | None, int | None]] = []
    for index in range(start, len(segments)):
        position, priority = segments[index]
        if position not in positions or not filled >> index & 1:
            continue
        before = None
        if cursor is not None and index == start:
            before = (cursor.sort_date, cursor.message_id)
        query = listing.newest_first(
            mailbox_ids,
            [*conditions, _in_segment(visible, position, priority)],
            before=before,
            limit=limit + 1 - len(rows),
        )
        category_id = visible[position] if position < len(visible) else None
        rows.extend((message, category_id, priority) for message in await session.scalars(query))
        if len(rows) > limit:
            break

    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last, category_id, priority = rows[-1]
        position = visible.index(category_id) if category_id is not None else len(visible)
        next_cursor = InboxCursor(position, priority, last.sort_date, last.id, filled)
    return InboxPage(rows=rows, next_cursor=next_cursor, total=total, counts=counts)

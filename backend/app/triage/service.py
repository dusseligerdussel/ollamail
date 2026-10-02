"""Triage of one message (pipeline step) and the database side of the API.

Functions take an open session and never commit.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import CloudLLMDisabledError, LLMGateway, LLMOutputError, LLMTask
from app.core.config import TriageSettings
from app.core.ids import uuid7
from app.mail.access import visible_to
from app.mail.models import Folder, FolderRole, Mailbox, Message, message_folders
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
    highest priority first, then newest; plus the total per group."""
    in_inbox = (
        select(message_folders.c.message_id)
        .join(Folder, Folder.id == message_folders.c.folder_id)
        .where(message_folders.c.message_id == Message.id, Folder.role == FolderRole.INBOX)
        .exists()
    )
    base = (
        select(Message, Mailbox.id, TriageResult)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .outerjoin(TriageResult, TriageResult.message_id == Message.id)
        .where(visible_to(user_id), in_inbox)
    )
    if mailbox_id is not None:
        base = base.where(Mailbox.id == mailbox_id)
    received = func.coalesce(Message.received_at, Message.sent_at, Message.created_at)
    groups: dict[uuid.UUID | None, list[InboxEntry]] = {}
    totals: dict[uuid.UUID | None, int] = {}
    for category_id in [*visible, None]:
        if category_id is None:
            statement = base.where(
                or_(
                    TriageResult.id.is_(None),
                    TriageResult.category_id.is_(None),
                    TriageResult.category_id.not_in(list(visible)),
                )
            )
        else:
            statement = base.where(TriageResult.category_id == category_id)
        count = await session.scalar(select(func.count()).select_from(statement.subquery()))
        totals[category_id] = int(count or 0)
        rows = await session.execute(
            statement.order_by(
                TriageResult.priority.asc().nulls_last(), received.desc(), Message.id
            ).limit(per_group)
        )
        groups[category_id] = [
            InboxEntry(
                message_id=message.id,
                mailbox_id=mailbox,
                subject=message.subject,
                sender_name=(message.sender or {}).get("name"),
                sender_address=(message.sender or {}).get("address"),
                received_at=message.received_at or message.sent_at,
                category_id=result.category_id if result is not None else None,
                priority=result.priority if result is not None else None,
                source=result.source if result is not None else None,
                reason=result.reason if result is not None else None,
            )
            for message, mailbox, result in rows
        ]
    return groups, totals

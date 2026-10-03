"""Triage API: categories, triage of a message, inbox by category, sender rules and the
per-mailbox write-back switch.

Every endpoint works on data the signed-in user may read (``app.mail.access``): messages
are only found in the user's own and assigned shared mailboxes, mailbox settings only for
own mailboxes (404 otherwise, so IDs of others are not confirmed).
Organisation categories are managed by admins, who never see mail contents here.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AdminSessionDep, CurrentSessionDep, SettingsDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.mail.access import MailboxPermission, get_mailbox
from app.mail.api.messages import _summary_fields as summary_fields
from app.mail.models import Mailbox
from app.triage import service
from app.triage.categories import EffectiveCategory, effective_categories
from app.triage.feedback import suggest_sender_rules
from app.triage.models import (
    TriageCategory,
    TriageCategoryPreference,
    TriageResult,
    TriageSenderRule,
)
from app.triage.schemas import (
    CategoryCount,
    CategoryCreate,
    CategoryOrder,
    CategoryRead,
    CategoryUpdate,
    InboxGroup,
    InboxMessage,
    MailboxTriageSettings,
    OrganizationCategoryCreate,
    OrganizationCategoryRead,
    OrganizationCategoryUpdate,
    SenderRuleCreate,
    SenderRuleRead,
    SenderRuleSuggestion,
    TriageCorrection,
    TriagedMessage,
    TriagedMessagePage,
    TriageRead,
)
from app.triage.writeback import set_write_back_mode, write_back_mode

log = get_logger(__name__)

router = APIRouter(
    prefix="/triage",
    tags=["triage"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "Not found or not yours"}}


def _read(category: EffectiveCategory) -> CategoryRead:
    return CategoryRead(
        id=category.id,
        name=category.name,
        description=category.description,
        builtin_key=category.builtin_key,
        scope="organization" if category.owner_user_id is None else "user",
        hidden=category.hidden,
        position=category.position,
    )


async def _preference(
    db: AsyncSession, user_id: uuid.UUID, category_id: uuid.UUID
) -> TriageCategoryPreference:
    preference = await db.scalar(
        select(TriageCategoryPreference).where(
            TriageCategoryPreference.user_id == user_id,
            TriageCategoryPreference.category_id == category_id,
        )
    )
    if preference is None:
        preference = TriageCategoryPreference(user_id=user_id, category_id=category_id)
        db.add(preference)
    return preference


async def _user_category(
    db: AsyncSession, user_id: uuid.UUID, category_id: uuid.UUID
) -> EffectiveCategory:
    for category in await effective_categories(db, user_id, include_hidden=True):
        if category.id == category_id:
            return category
    raise ProblemError(404, detail="Category not found.")


# -- categories of the user ------------------------------------------------------------


@router.get("/categories")
async def list_categories(current: CurrentSessionDep, db: DbDep) -> list[CategoryRead]:
    """Organisation and own categories in the user's order, hidden ones included."""
    categories = await effective_categories(db, current.user_id, include_hidden=True)
    return [_read(category) for category in categories]


@router.post("/categories", status_code=status.HTTP_201_CREATED)
async def create_category(
    body: CategoryCreate, current: CurrentSessionDep, db: DbDep
) -> CategoryRead:
    """Create an own category; the description tells the model what belongs in it."""
    category = TriageCategory(
        owner_user_id=current.user_id,
        name=body.name,
        description=body.description,
        position=1000,
    )
    db.add(category)
    await db.flush()
    created = await _user_category(db, current.user_id, category.id)
    await db.commit()
    return _read(created)


@router.patch("/categories/{category_id}", responses=NOT_FOUND)
async def update_category(
    category_id: uuid.UUID, body: CategoryUpdate, current: CurrentSessionDep, db: DbDep
) -> CategoryRead:
    """Rename or describe an own category, or hide/show any category."""
    effective = await _user_category(db, current.user_id, category_id)
    if body.name is not None or body.description is not None:
        if effective.owner_user_id is None:
            raise ProblemError(403, detail="Organisation categories are managed by admins.")
        category = await db.get(TriageCategory, category_id)
        assert category is not None
        if body.name is not None:
            category.name = body.name.strip()
        if body.description is not None:
            category.description = body.description
    if body.hidden is not None:
        if body.hidden and not effective.hidden:
            visible = await effective_categories(db, current.user_id)
            if len(visible) <= 1:
                raise ProblemError(409, detail="At least one category must stay visible.")
        (await _preference(db, current.user_id, category_id)).hidden = body.hidden
    await db.flush()
    updated = await _user_category(db, current.user_id, category_id)
    await db.commit()
    return _read(updated)


@router.delete(
    "/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND
)
async def delete_category(category_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> None:
    """Delete an own category. Its messages become uncategorised; corrections and sender
    rules pointing to it are deleted."""
    result = await db.execute(
        delete(TriageCategory).where(
            TriageCategory.id == category_id, TriageCategory.owner_user_id == current.user_id
        )
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise ProblemError(404, detail="Category not found.")
    await db.commit()


@router.put("/categories/order", responses={422: {"description": "Not all categories"}})
async def order_categories(
    body: CategoryOrder, current: CurrentSessionDep, db: DbDep
) -> list[CategoryRead]:
    """Set the order of all of the user's categories."""
    categories = await effective_categories(db, current.user_id, include_hidden=True)
    if sorted(body.category_ids) != sorted(c.id for c in categories):
        raise ProblemError(422, detail="The order must list each category exactly once.")
    for position, category_id in enumerate(body.category_ids):
        (await _preference(db, current.user_id, category_id)).position = position
    await db.flush()
    ordered = await effective_categories(db, current.user_id, include_hidden=True)
    await db.commit()
    return [_read(category) for category in ordered]


# -- organisation categories (admin) ---------------------------------------------------

ADMIN_ONLY: dict[int | str, dict[str, Any]] = {403: {"description": "Not an admin"}}


@router.get("/organization/categories", responses=ADMIN_ONLY)
async def list_organization_categories(
    _: AdminSessionDep, db: DbDep
) -> list[OrganizationCategoryRead]:
    """Organisation defaults offered to every user."""
    categories = await db.scalars(
        select(TriageCategory)
        .where(TriageCategory.owner_user_id.is_(None))
        .order_by(TriageCategory.position, TriageCategory.name)
    )
    return [OrganizationCategoryRead.model_validate(c) for c in categories]


@router.post("/organization/categories", status_code=status.HTTP_201_CREATED, responses=ADMIN_ONLY)
async def create_organization_category(
    body: OrganizationCategoryCreate, admin: AdminSessionDep, db: DbDep
) -> OrganizationCategoryRead:
    category = TriageCategory(
        owner_user_id=None,
        name=body.name,
        description=body.description,
        position=body.position,
    )
    db.add(category)
    await db.commit()
    log.info("triage_org_category_created", category_id=category.id, by_user_id=admin.user_id)
    return OrganizationCategoryRead.model_validate(category)


async def _organization_category(db: AsyncSession, category_id: uuid.UUID) -> TriageCategory:
    category = await db.get(TriageCategory, category_id)
    if category is None or category.owner_user_id is not None:
        raise ProblemError(404, detail="Category not found.")
    return category


@router.patch("/organization/categories/{category_id}", responses={**ADMIN_ONLY, **NOT_FOUND})
async def update_organization_category(
    category_id: uuid.UUID, body: OrganizationCategoryUpdate, admin: AdminSessionDep, db: DbDep
) -> OrganizationCategoryRead:
    category = await _organization_category(db, category_id)
    if body.name is not None and body.name.strip() != category.name:
        category.name = body.name.strip()
        # A renamed default is no longer translated by the UI.
        category.builtin_key = None
    if body.description is not None:
        category.description = body.description
    if body.position is not None:
        category.position = body.position
    await db.commit()
    log.info("triage_org_category_updated", category_id=category.id, by_user_id=admin.user_id)
    return OrganizationCategoryRead.model_validate(category)


@router.delete(
    "/organization/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**ADMIN_ONLY, **NOT_FOUND},
)
async def delete_organization_category(
    category_id: uuid.UUID, admin: AdminSessionDep, db: DbDep
) -> None:
    category = await _organization_category(db, category_id)
    await db.delete(category)
    await db.commit()
    log.info("triage_org_category_deleted", category_id=category_id, by_user_id=admin.user_id)


# -- triage of messages ----------------------------------------------------------------


def _triage_read(result: TriageResult) -> TriageRead:
    return TriageRead(
        message_id=result.message_id,
        category_id=result.category_id,
        priority=result.priority,
        reason=result.reason,
        rule=result.rule,
        source=result.source,
        model=result.model,
        prompt_version=result.prompt_version,
        updated_at=result.updated_at,
    )


@router.get("/messages")
async def list_triage(
    current: CurrentSessionDep,
    db: DbDep,
    ids: Annotated[list[uuid.UUID], Query(min_length=1, max_length=200)],
) -> list[TriageRead]:
    """Triage of several messages at once (e.g. the visible rows of a list). Messages that
    are not triaged yet, or not the user's, are left out."""
    return [_triage_read(r) for r in await service.results_of(db, current.user_id, ids)]


@router.get("/messages/{message_id}", responses=NOT_FOUND)
async def get_triage(message_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> TriageRead:
    """Category, priority and reason of a message (404 while it is not triaged yet)."""
    if await service.readable_message(db, current.user_id, message_id) is None:
        raise ProblemError(404, detail="Message not found.")
    result = await db.scalar(select(TriageResult).where(TriageResult.message_id == message_id))
    if result is None:
        raise ProblemError(404, detail="Message not triaged yet.")
    return _triage_read(result)


@router.put("/messages/{message_id}", responses=NOT_FOUND)
async def correct_triage(
    message_id: uuid.UUID, body: TriageCorrection, current: CurrentSessionDep, db: DbDep
) -> TriageRead:
    """Correct category and priority. The correction is kept on reprocessing and used as
    example for this user's future classifications. In a shared mailbox it applies to
    everybody who reads the mailbox, only organisation categories can be chosen, and it
    serves as example for the future classifications of that mailbox."""
    found = await service.readable_message(db, current.user_id, message_id)
    if found is None:
        raise ProblemError(404, detail="Message not found.")
    message, mailbox = found
    visible = {c.id for c in await effective_categories(db, current.user_id)}
    if mailbox.is_shared:
        visible &= {c.id for c in await effective_categories(db, None)}
    if body.category_id not in visible:
        raise ProblemError(422, detail="Unknown or hidden category.")
    result = await service.correct(db, current.user_id, message_id, body.category_id, body.priority)
    await service.publish_triaged(db, message_id, message.mailbox_id)
    await db.commit()
    await db.refresh(result)
    log.info("triage_corrected", message_id=message_id, user_id=current.user_id)
    return _triage_read(result)


@router.get("/inbox")
async def get_inbox(
    current: CurrentSessionDep,
    db: DbDep,
    mailbox_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200, description="Messages per group")] = 50,
) -> list[InboxGroup]:
    """Inbox of the user's mailboxes grouped by visible category, in the user's order;
    the last group (``category: null``) holds messages without a visible category."""
    categories = await effective_categories(db, current.user_id)
    groups, totals = await service.inbox(
        db,
        current.user_id,
        [c.id for c in categories],
        mailbox_id=mailbox_id,
        per_group=limit,
    )
    by_id = {c.id: c for c in categories}
    response = []
    for category_id, entries in groups.items():
        category = by_id.get(category_id) if category_id is not None else None
        response.append(
            InboxGroup(
                category=_read(category) if category is not None else None,
                total=totals[category_id],
                messages=[
                    InboxMessage(
                        message_id=e.message_id,
                        mailbox_id=e.mailbox_id,
                        subject=e.subject,
                        sender_name=e.sender_name,
                        sender_address=e.sender_address,
                        received_at=e.received_at,
                        priority=e.priority,
                        source=e.source,
                        reason=e.reason,
                    )
                    for e in entries
                ],
            )
        )
    return response


@router.get(
    "/inbox/messages",
    responses={422: {"description": "Unknown or hidden category, invalid cursor"}},
)
async def list_inbox_messages(
    current: CurrentSessionDep,
    db: DbDep,
    mailbox_id: uuid.UUID | None = None,
    unread: Annotated[bool | None, Query(description="Only unread (true) or read (false)")] = None,
    category: Annotated[
        uuid.UUID | Literal["none"] | None,
        Query(description="One visible category, or `none` for the uncategorised messages"),
    ] = None,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> TriagedMessagePage:
    """Inbox messages ordered by category (user's order, uncategorised last), then
    priority, then newest first. Pages with ``cursor``; the first page (without ``cursor``)
    also carries the total and the number of messages per category."""
    categories = await effective_categories(db, current.user_id)
    visible = [c.id for c in categories]
    if isinstance(category, uuid.UUID) and category not in visible:
        raise ProblemError(422, detail="Unknown or hidden category.")
    try:
        position = service.InboxCursor.decode(cursor) if cursor is not None else None
    except ValueError:
        raise ProblemError(422, detail="Invalid cursor.", error_code="invalid_cursor") from None
    page = await service.inbox_page(
        db,
        current.user_id,
        visible,
        mailbox_id=mailbox_id,
        unread=unread,
        category=category,
        cursor=position,
        limit=limit,
    )
    return TriagedMessagePage(
        items=[
            TriagedMessage(**summary_fields(message), category_id=category_id, priority=priority)
            for message, category_id, priority in page.rows
        ],
        next_cursor=page.next_cursor.encode() if page.next_cursor is not None else None,
        total=page.total,
        groups=[
            CategoryCount(category_id=category_id, total=total)
            for category_id, total in page.counts.items()
        ]
        if page.counts is not None
        else None,
    )


# -- sender rules ----------------------------------------------------------------------


@router.get("/sender-rules")
async def list_sender_rules(current: CurrentSessionDep, db: DbDep) -> list[SenderRuleRead]:
    rules = await db.scalars(
        select(TriageSenderRule)
        .where(TriageSenderRule.user_id == current.user_id)
        .order_by(TriageSenderRule.sender)
    )
    return [SenderRuleRead.model_validate(rule) for rule in rules]


@router.post(
    "/sender-rules",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Rule for this sender exists"}},
)
async def create_sender_rule(
    body: SenderRuleCreate, current: CurrentSessionDep, db: DbDep
) -> SenderRuleRead:
    """ "Always <category>" for an address or a domain; applied before the model."""
    await _user_category(db, current.user_id, body.category_id)
    rule = TriageSenderRule(
        user_id=current.user_id,
        sender=body.sender,
        category_id=body.category_id,
        priority=body.priority,
    )
    db.add(rule)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ProblemError(409, detail="A rule for this sender exists.") from None
    await db.commit()
    return SenderRuleRead.model_validate(rule)


@router.delete(
    "/sender-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND
)
async def delete_sender_rule(rule_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> None:
    result = await db.execute(
        delete(TriageSenderRule).where(
            TriageSenderRule.id == rule_id, TriageSenderRule.user_id == current.user_id
        )
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise ProblemError(404, detail="Rule not found.")
    await db.commit()


@router.get("/sender-rules/suggestions")
async def list_sender_rule_suggestions(
    current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> list[SenderRuleSuggestion]:
    """Senders the user corrected repeatedly into the same category."""
    suggestions = await suggest_sender_rules(
        db,
        current.user_id,
        min_corrections=settings.triage.rule_suggestion_min_corrections,
    )
    return [
        SenderRuleSuggestion(
            sender=s.sender,
            category_id=s.category_id,
            priority=s.priority,
            corrections=s.corrections,
        )
        for s in suggestions
    ]


# -- write-back per mailbox ------------------------------------------------------------


async def _owned_mailbox(db: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID) -> Mailbox:
    mailbox = await get_mailbox(db, user_id, mailbox_id, MailboxPermission.MANAGE)
    if mailbox is None:
        raise ProblemError(404, detail="Mailbox not found.")
    return mailbox


@router.get("/mailboxes/{mailbox_id}/settings", responses=NOT_FOUND)
async def get_mailbox_settings(
    mailbox_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> MailboxTriageSettings:
    await _owned_mailbox(db, current.user_id, mailbox_id)
    return MailboxTriageSettings(write_back=await write_back_mode(db, mailbox_id))


@router.put(
    "/mailboxes/{mailbox_id}/settings",
    responses=NOT_FOUND,
)
async def update_mailbox_settings(
    mailbox_id: uuid.UUID,
    body: MailboxTriageSettings,
    current: CurrentSessionDep,
    db: DbDep,
) -> MailboxTriageSettings:
    """Write the category back to the server (keyword/label or folder). Enabling it also
    labels the messages triaged so far, in batches."""
    await _owned_mailbox(db, current.user_id, mailbox_id)
    await set_write_back_mode(db, mailbox_id, body.write_back)
    await db.commit()
    log.info("triage_write_back_changed", mailbox_id=mailbox_id, mode=body.write_back)
    return body

"""Filters from the question: the model proposes, the code decides.

The model gets the user's questions (never mail content) plus the keys of the user's
mailboxes and categories, and returns a standalone search query and optional filters.
Values are only accepted if they map to something the user may use: a mailbox key to a
readable mailbox, a category key to a visible category, dates to a valid period. Filters
set in the UI always win; extracted ones only fill the gaps.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.models import Mailbox
from app.rag.schemas import AppliedFilters, FilterField, RagFilters
from app.search.access import readable_mailbox_ids
from app.search.service import SearchFilters
from app.triage.categories import effective_categories

_WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
}
_SEARCH_QUERY_MAX = 500


class QueryAnalysis(BaseModel):
    """Structured output of the ``rag_query`` prompt."""

    search_query: str = ""
    since: date | None = None
    until: date | None = None
    sender: str | None = Field(default=None, max_length=200)
    mailbox: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True)
class Choice:
    """A mailbox or category the model may name by ``key``."""

    key: str
    id: uuid.UUID
    label: str


@dataclass(frozen=True)
class QueryContext:
    mailboxes: list[Choice]
    categories: list[Choice]
    timezone: tzinfo


def user_timezone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


async def query_context(
    session: AsyncSession, user_id: uuid.UUID, timezone: str | None
) -> QueryContext:
    """Readable mailboxes (access rule in SQL) and visible categories of ``user_id``."""
    mailboxes = (
        await session.execute(
            select(Mailbox.id, Mailbox.display_name, Mailbox.address)
            .where(Mailbox.id.in_(readable_mailbox_ids(user_id)))
            .order_by(Mailbox.display_name, Mailbox.id)
        )
    ).all()
    categories = await effective_categories(session, user_id)
    return QueryContext(
        mailboxes=[
            Choice(f"m{index}", row.id, f"{row.display_name} <{row.address}>")
            for index, row in enumerate(mailboxes, start=1)
        ],
        categories=[Choice(c.key, c.id, c.name) for c in categories],
        timezone=user_timezone(timezone),
    )


def today(context: QueryContext, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(context.timezone).date()


def weekday(day: date, language: str) -> str:
    return _WEEKDAYS.get(language, _WEEKDAYS["en"])[day.weekday()]


def choices_text(choices: Sequence[Choice], none: str) -> str:
    return "\n".join(f"- {c.key}: {c.label}" for c in choices) or none


def _start_of(day: date, timezone: tzinfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=timezone).astimezone(UTC)


def _find(choices: Sequence[Choice], key: str | None) -> Choice | None:
    if not key:
        return None
    wanted = key.strip().lower()
    return next((c for c in choices if c.key.lower() == wanted), None)


def search_query(question: str, analysis: QueryAnalysis | None) -> str:
    """The text to search for: the model's standalone query, else the question."""
    query = " ".join((analysis.search_query if analysis else "").split())
    return query[:_SEARCH_QUERY_MAX] or question


def combine(
    ui: RagFilters, analysis: QueryAnalysis | None, context: QueryContext
) -> AppliedFilters:
    """UI filters, completed by valid filters from the question."""
    applied = AppliedFilters.model_validate(ui.model_dump())
    if analysis is None:
        return applied
    extracted: list[FilterField] = []
    mailbox = _find(context.mailboxes, analysis.mailbox)
    if applied.mailbox_ids is None and mailbox is not None:
        applied.mailbox_ids = [mailbox.id]
        extracted.append("mailbox_ids")
    category = _find(context.categories, analysis.category)
    if applied.category_ids is None and category is not None:
        applied.category_ids = [category.id]
        extracted.append("category_ids")
    sender = " ".join((analysis.sender or "").split())
    if applied.sender is None and sender:
        applied.sender = sender
        extracted.append("sender")
    since, until = analysis.since, analysis.until
    if since is not None and until is not None and since > until:
        since = until = None
    if applied.since is None and applied.until is None:
        if since is not None:
            applied.since = _start_of(since, context.timezone)
            extracted.append("since")
        if until is not None:
            # ``until`` is inclusive in the prompt, exclusive in the search.
            applied.until = _start_of(until + timedelta(days=1), context.timezone)
            extracted.append("until")
    applied.extracted = extracted
    return applied


def search_filters(applied: RagFilters) -> SearchFilters:
    return SearchFilters(
        mailbox_ids=applied.mailbox_ids,
        folder_ids=applied.folder_ids,
        category_ids=applied.category_ids,
        sender=applied.sender,
        since=applied.since,
        until=applied.until,
    )

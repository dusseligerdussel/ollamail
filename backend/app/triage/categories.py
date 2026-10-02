"""Categories: built-in defaults, and the effective list of one user.

The effective list is what a user sees and what the model may choose from: the
organisation categories plus the user's own, in the user's order, with the user's
hidden flags. Each category gets a short ``key`` (``newsletter``, ``project_x``) that the
model answers with; keys are derived per classification and never stored.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.triage.models import TriageCategory, TriageCategoryPreference


@dataclass(frozen=True, slots=True)
class DefaultCategory:
    key: str
    name: str
    description: str


# Organisation defaults (docs/ARCHITECTURE.md §4.2), created by the migration. Names and
# descriptions are English: the descriptions are part of the prompt, the UI translates
# built-in names by key.
DEFAULT_CATEGORIES: tuple[DefaultCategory, ...] = (
    DefaultCategory(
        "important",
        "Important",
        "Personal mail that matters to the recipient and should be read soon, e.g. from "
        "colleagues, customers or family, or about deadlines, contracts, money or health.",
    ),
    DefaultCategory(
        "action_required",
        "Action required",
        "The sender expects the recipient to do something: reply, decide, approve, pay, "
        "sign, fill in a form or schedule a meeting.",
    ),
    DefaultCategory(
        "waiting_for",
        "Waiting for",
        "Answers or updates on something the recipient started and is waiting for, e.g. "
        "replies to the recipient's questions, order or booking confirmations, status "
        "updates on open requests.",
    ),
    DefaultCategory(
        "info",
        "Info",
        "Personal or work mail that only informs and needs no action, e.g. FYI messages, "
        "meeting minutes, announcements to a team.",
    ),
    DefaultCategory(
        "newsletter",
        "Newsletter",
        "Newsletters, mailing lists, digests, blogs and other subscribed bulk content.",
    ),
    DefaultCategory(
        "notification",
        "Notification",
        "Automated messages from systems and services, e.g. account and security alerts, "
        "shipping updates, invoices sent automatically, calendar, ticket or build "
        "notifications.",
    ),
    DefaultCategory(
        "spam",
        "Spam/Advertising",
        "Unsolicited advertising, marketing offers, scams, phishing and other spam.",
    ),
)

_NON_KEY = re.compile(r"[^a-z0-9]+")
KEY_MAX_LENGTH = 32


def slugify(name: str) -> str:
    """ASCII key for a category name: ``Projekt Übersicht`` → ``projekt_ubersicht``."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    key = _NON_KEY.sub("_", ascii_name.lower()).strip("_")[:KEY_MAX_LENGTH].strip("_")
    return key or "category"


@dataclass(frozen=True, slots=True)
class EffectiveCategory:
    id: uuid.UUID
    # Key the model answers with; unique within one effective list.
    key: str
    name: str
    description: str
    builtin_key: str | None
    # ``None`` for organisation categories.
    owner_user_id: uuid.UUID | None
    position: int
    hidden: bool


async def effective_categories(
    session: AsyncSession, user_id: uuid.UUID | None, *, include_hidden: bool = False
) -> list[EffectiveCategory]:
    """Categories of ``user_id`` (organisation ones only for ``None``), in the user's order.

    Organisation categories come before own ones unless the user reordered them.
    """
    ownership: ColumnElement[bool] = TriageCategory.owner_user_id.is_(None)
    if user_id is not None:
        ownership = or_(ownership, TriageCategory.owner_user_id == user_id)
    categories = list(await session.scalars(select(TriageCategory).where(ownership)))
    preferences: dict[uuid.UUID, TriageCategoryPreference] = {}
    if user_id is not None:
        preferences = {
            p.category_id: p
            for p in await session.scalars(
                select(TriageCategoryPreference).where(TriageCategoryPreference.user_id == user_id)
            )
        }

    def order(category: TriageCategory) -> tuple[int, int, int, str, str]:
        preference = preferences.get(category.id)
        if preference is not None and preference.position is not None:
            return (0, preference.position, 0, category.name.lower(), str(category.id))
        own = 0 if category.owner_user_id is None else 1
        return (1, own, category.position, category.name.lower(), str(category.id))

    ordered = sorted(categories, key=order)
    # Built-in categories keep their key; the others are numbered on clashes, in a fixed
    # order so that keys do not depend on hidden flags.
    keys: dict[uuid.UUID, str] = {}
    used: set[str] = set()
    for category in sorted(ordered, key=lambda c: (c.builtin_key is None, str(c.id))):
        base = category.builtin_key or slugify(category.name)
        key, suffix = base, 2
        while key in used:
            key = f"{base[: KEY_MAX_LENGTH - 3]}_{suffix}"
            suffix += 1
        used.add(key)
        keys[category.id] = key

    result: list[EffectiveCategory] = []
    for position, category in enumerate(ordered):
        preference = preferences.get(category.id)
        hidden = preference is not None and preference.hidden
        if hidden and not include_hidden:
            continue
        result.append(
            EffectiveCategory(
                id=category.id,
                key=keys[category.id],
                name=category.name,
                description=category.description,
                builtin_key=category.builtin_key,
                owner_user_id=category.owner_user_id,
                position=position,
                hidden=hidden,
            )
        )
    return result

"""Message lists newest first, paged with a keyset (#140).

Lists are ordered by ``Message.sort_date`` (received, else sent, else stored) and ``id``,
both descending. The index ``ix_mail_messages_mailbox_id_sort_date_id`` serves this order per
mailbox, so a page reads about ``limit`` index entries instead of sorting every matching
message. Lists over several mailboxes read each mailbox on its own and merge the parts.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select, exists, false, func, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.orm.interfaces import LoaderOption

from app.mail import access
from app.mail.models import Folder, FolderRole, Mailbox, Message, message_folders

# Columns a list row never shows; ``body_main`` stays for the snippet.
_UNLISTED = (
    Message.body_text,
    Message.body_html,
    Message.headers,
    Message.signature,
    Message.references,
)

NEWEST_FIRST = (Message.sort_date.desc(), Message.id.desc())


def without_bodies() -> list[LoaderOption]:
    """Loader options that skip the columns a list does not need."""
    return [defer(column) for column in _UNLISTED]


async def readable_mailbox_ids(
    session: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """The mailboxes ``user_id`` may read (only ``mailbox_id`` if given), in a fixed order."""
    query = select(Mailbox.id).where(access.visible_to(user_id)).order_by(Mailbox.id)
    if mailbox_id is not None:
        query = query.where(Mailbox.id == mailbox_id)
    return list(await session.scalars(query))


async def folder_ids(
    session: AsyncSession, mailbox_ids: Sequence[uuid.UUID], role: FolderRole
) -> list[uuid.UUID]:
    """The folders with ``role`` of ``mailbox_ids``."""
    if not mailbox_ids:
        return []
    query = select(Folder.id).where(Folder.mailbox_id.in_(mailbox_ids), Folder.role == role)
    return list(await session.scalars(query.order_by(Folder.id)))


def in_folders(ids: Sequence[uuid.UUID]) -> ColumnElement[bool]:
    """The message is in one of the folders ``ids``.

    Takes folder IDs rather than a join on ``mail_folders``: that table is small and often
    without statistics, and a misestimated join makes the planner sort all messages instead
    of reading the index in list order.
    """
    if not ids:
        return false()
    return exists(
        select(message_folders.c.message_id).where(
            message_folders.c.message_id == Message.id, message_folders.c.folder_id.in_(ids)
        )
    )


def newest_first(
    mailbox_ids: Sequence[uuid.UUID],
    conditions: Sequence[ColumnElement[bool]],
    *,
    before: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> Select[Message]:
    """At most ``limit`` messages of ``mailbox_ids`` matching ``conditions``, newest first,
    older than ``before`` (``sort_date``, ``id``) if given. Without bodies."""
    keyset = list(conditions)
    if before is not None:
        keyset.append(tuple_(Message.sort_date, Message.id) < tuple_(*before))
    if not mailbox_ids:
        return select(Message).where(false()).limit(0)
    if len(mailbox_ids) == 1:
        query = select(Message).where(Message.mailbox_id == mailbox_ids[0], *keyset)
    else:
        # One index range per mailbox; ``mailbox_id IN (...)`` would sort all their messages.
        parts = [
            select(Message.id)
            .where(Message.mailbox_id == mailbox_id, *keyset)
            .order_by(*NEWEST_FIRST)
            .limit(limit)
            .subquery()
            for mailbox_id in mailbox_ids
        ]
        ids = union_all(*(select(part.c.id) for part in parts)).subquery()
        query = select(Message).join(ids, ids.c.id == Message.id)
    return query.options(*without_bodies()).order_by(*NEWEST_FIRST).limit(limit)


async def count(
    session: AsyncSession,
    mailbox_ids: Sequence[uuid.UUID],
    conditions: Sequence[ColumnElement[bool]],
) -> int:
    """Number of messages of ``mailbox_ids`` matching ``conditions``."""
    if not mailbox_ids:
        return 0
    matching = select(Message.id).where(Message.mailbox_id.in_(mailbox_ids), *conditions)
    return await session.scalar(select(func.count()).select_from(matching.subquery())) or 0

"""Message lists newest first, paged with a keyset (#140).

Lists are ordered by ``Message.sort_date`` (received, else sent, else stored) and ``id``,
both descending. The index ``ix_mail_messages_mailbox_id_sort_date_id`` serves this order per
mailbox, so a page reads about ``limit`` index entries instead of sorting every matching
message; ``ix_mail_messages_unread`` does the same for unread messages only (#186). Lists over
several mailboxes read each mailbox on its own and merge the parts. Counts start from the
folder membership (``mail_message_folders``), so they read the folder, not the mailbox.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    Select,
    Text,
    exists,
    false,
    func,
    literal_column,
    select,
    tuple_,
    union_all,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, with_expression
from sqlalchemy.orm.interfaces import LoaderOption

from app.mail import access
from app.mail.models import Folder, FolderRole, Mailbox, Message, message_folders
from app.mail.providers.base import Flag

# Columns a list row never shows.
_UNLISTED = (
    Message.body_text,
    Message.body_html,
    Message.body_main,
    Message.headers,
    Message.signature,
    Message.references,
)
# Characters of ``body_main`` a list row reads for its snippet (``Message.snippet``).
SNIPPET_LENGTH = 200

NEWEST_FIRST = (Message.sort_date.desc(), Message.id.desc())

# A literal, not a parameter: only then can the planner match the predicate of the partial
# index ``ix_mail_messages_unread`` (``app.mail.models.UNREAD_PREDICATE``).
_SEEN = literal_column(f"ARRAY['{Flag.SEEN.value}']::text[]", ARRAY(Text))
UNREAD: ColumnElement[bool] = ~Message.flags.contains(_SEEN)
READ: ColumnElement[bool] = Message.flags.contains(_SEEN)


def read_state(unread: bool | None) -> list[ColumnElement[bool]]:
    """Conditions for the ``unread`` filter of the lists: only unread, only read, or all."""
    if unread is None:
        return []
    return [UNREAD if unread else READ]


def without_bodies() -> list[LoaderOption]:
    """Loader options that skip the columns a list does not need; ``Message.snippet`` gets
    the start of ``body_main`` instead of the whole text."""
    snippet = func.left(Message.body_main, SNIPPET_LENGTH)
    return [*(defer(column) for column in _UNLISTED), with_expression(Message.snippet, snippet)]


async def readable_mailbox_ids(
    session: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """The mailboxes ``user_id`` may read (only ``mailbox_id`` if given), in a fixed order."""
    query = select(Mailbox.id).where(access.visible_to(user_id)).order_by(Mailbox.id)
    if mailbox_id is not None:
        query = query.where(Mailbox.id == mailbox_id)
    return list(await session.scalars(query))


async def folder_ids(
    session: AsyncSession, mailbox_ids: Sequence[uuid.UUID], role: FolderRole | uuid.UUID
) -> list[uuid.UUID]:
    """The folders with ``role`` of ``mailbox_ids``, or the folder with that ID if it belongs
    to one of them."""
    if not mailbox_ids:
        return []
    query = select(Folder.id).where(Folder.mailbox_id.in_(mailbox_ids))
    if isinstance(role, FolderRole):
        query = query.where(Folder.role == role)
    else:
        query = query.where(Folder.id == role)
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


def folder_members(
    folders: Sequence[uuid.UUID], conditions: Sequence[ColumnElement[bool]] = ()
) -> Select[tuple[uuid.UUID]]:
    """IDs of the messages in ``folders`` matching ``conditions`` (on ``Message``), each once.

    ``folders`` must belong to mailboxes the user may read (``folder_ids``). Starts from the
    folder index, so it reads the folders' messages, not the whole mailbox; without
    ``conditions`` it never touches ``mail_messages``."""
    if not folders:
        return select(message_folders.c.message_id).where(false())
    query = select(message_folders.c.message_id).where(message_folders.c.folder_id.in_(folders))
    if conditions:
        query = query.join(Message, Message.id == message_folders.c.message_id).where(*conditions)
    # A message can carry several of the folders (Gmail labels).
    return query.distinct() if len(folders) > 1 else query


async def count(
    session: AsyncSession,
    folders: Sequence[uuid.UUID],
    conditions: Sequence[ColumnElement[bool]] = (),
) -> int:
    """Number of messages in ``folders`` (see ``folder_members``) matching ``conditions``."""
    if not folders:
        return 0
    members = folder_members(folders, conditions).subquery()
    return await session.scalar(select(func.count()).select_from(members)) or 0

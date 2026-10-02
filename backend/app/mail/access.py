"""Who may read which mailbox: the single place for this rule (#34).

Every query on mail data or anything derived from it (inbox, threads, attachments,
triage, todos, search, RAG, digest) restricts mailboxes with
``accessible_mailbox_ids(user_id)``, in SQL (docs/PRIVACY.md: access control on the server,
never in a prompt or in Python after the fact). Nothing caches the result, so revoking an
assignment takes effect with the next request.

A user may read

* the mailboxes they own (``owner_user_id``), and
* shared mailboxes assigned to them directly or to one of their groups
  (``MailboxAssignment``). Groups are the ones an identity of the user reported at its
  last login (``auth_identities.groups``), compared case-insensitively.

Admins get no implicit access: they manage shared mailboxes (connection, assignments)
but read their mails only if assigned like anybody else (docs/PRIVACY.md, Admin ≠ Leser).

Mailbox endpoints use ``get_mailbox`` with a ``MailboxPermission``: owners hold all of
them, assigned users ``READ`` only. A mailbox without access behaves like a missing one
(404), so IDs of other mailboxes cannot be probed.
"""

import enum
import uuid

from sqlalchemy import (
    ColumnElement,
    Select,
    SQLColumnExpression,
    and_,
    exists,
    func,
    or_,
    select,
    true,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Identity
from app.core.events import Event, publish
from app.mail.models import Mailbox, MailboxAssignment
from app.users.models import User


class MailboxPermission(enum.StrEnum):
    # See the mailbox, its folders, its sync status and its mails.
    READ = "read"
    # Trigger a sync.
    SYNC = "sync"
    # Change settings, credentials and folder selection; remove the mailbox.
    MANAGE = "manage"
    # Act on mails, e.g. mark read/unread (written back to the server). Not yet granted
    # by assignments to shared mailboxes.
    ACT = "act"


def _assigned(user_id: uuid.UUID | SQLColumnExpression[uuid.UUID]) -> ColumnElement[bool]:
    """``Mailbox`` is assigned to ``user_id``, directly or through one of their groups."""
    groups = (
        func.unnest(Identity.groups).table_valued("name").render_derived("identity_group").lateral()
    )
    via_group = exists(
        select(Identity.id)
        .join(groups, true())
        .where(
            Identity.user_id == user_id,
            func.lower(groups.c.name) == func.lower(MailboxAssignment.group_name),
            or_(
                MailboxAssignment.provider.is_(None),
                MailboxAssignment.provider == Identity.provider,
            ),
        )
        # Explicit: ``user_id`` may be a column of an enclosing query (``readers``).
        .correlate_except(Identity, groups)
    )
    return exists(
        select(MailboxAssignment.id)
        .where(
            MailboxAssignment.mailbox_id == Mailbox.id,
            or_(
                MailboxAssignment.user_id == user_id,
                and_(MailboxAssignment.group_name.is_not(None), via_group),
            ),
        )
        .correlate_except(MailboxAssignment)
    )


def _readable(user_id: uuid.UUID | SQLColumnExpression[uuid.UUID]) -> ColumnElement[bool]:
    return or_(Mailbox.owner_user_id == user_id, and_(Mailbox.is_shared, _assigned(user_id)))


def accessible_mailbox_ids(user_id: uuid.UUID) -> Select[uuid.UUID]:
    """Subquery of the IDs of all mailboxes ``user_id`` may read.

    Use it as ``<table>.mailbox_id.in_(accessible_mailbox_ids(user_id))``."""
    return select(Mailbox.id).where(_readable(user_id))


def visible_to(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Filter on ``Mailbox`` for the mailboxes ``user_id`` may read."""
    return Mailbox.id.in_(accessible_mailbox_ids(user_id))


def permissions(mailbox: Mailbox, user_id: uuid.UUID) -> frozenset[MailboxPermission]:
    """Permissions on a mailbox already known to be readable by ``user_id``."""
    if mailbox.owner_user_id == user_id:
        return frozenset(MailboxPermission)
    # Assigned shared mailbox: read only for now; actions follow later (#34).
    return frozenset({MailboxPermission.READ})


async def get_mailbox(
    session: AsyncSession,
    user_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    permission: MailboxPermission = MailboxPermission.READ,
) -> Mailbox | None:
    """The mailbox if ``user_id`` holds ``permission`` on it, else ``None``."""
    mailbox = await session.scalar(
        select(Mailbox).where(Mailbox.id == mailbox_id, visible_to(user_id))
    )
    if mailbox is None or permission not in permissions(mailbox, user_id):
        return None
    return mailbox


def readers(mailbox_id: uuid.UUID) -> Select[uuid.UUID]:
    """Subquery of the active users who may read ``mailbox_id`` (owner or assigned):
    recipients of its events, candidates for its todos."""
    return select(User.id).where(
        User.is_active,
        exists(select(Mailbox.id).where(Mailbox.id == mailbox_id, _readable(User.id))),
    )


async def reader_ids(session: AsyncSession, mailbox_id: uuid.UUID) -> list[uuid.UUID]:
    return list(await session.scalars(readers(mailbox_id).order_by(User.id)))


async def can_read(session: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(Mailbox.id).where(Mailbox.id == mailbox_id, visible_to(user_id))
    )
    return found is not None


async def publish_to_readers(
    session: AsyncSession, mailbox: Mailbox | uuid.UUID, event: Event
) -> None:
    """Queue ``event`` for everybody who may read the mailbox: the owner, or the users
    assigned to a shared mailbox (docs/PRIVACY.md: events only reach those concerned)."""
    if isinstance(mailbox, Mailbox) and mailbox.owner_user_id is not None:
        await publish(session, mailbox.owner_user_id, event)
        return
    mailbox_id = mailbox.id if isinstance(mailbox, Mailbox) else mailbox
    for user_id in await reader_ids(session, mailbox_id):
        await publish(session, user_id, event)

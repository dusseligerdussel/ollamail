"""Who may see and change a mailbox.

Every mailbox endpoint goes through ``get_mailbox`` (or filters with ``visible_to``), so the
access rules live in one place. Today only the owner has access. Shared mailboxes (#34)
extend these two functions: assigned users and groups get ``READ`` (and possibly
``SYNC``), while ``MANAGE`` stays with admins/owners. A mailbox without access behaves
like a missing one (404), so IDs of other users' mailboxes cannot be probed.
"""

import enum
import uuid

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.models import Mailbox


class MailboxPermission(enum.StrEnum):
    # See the mailbox, its folders and its sync status.
    READ = "read"
    # Trigger a sync.
    SYNC = "sync"
    # Change settings, credentials and folder selection; remove the mailbox.
    MANAGE = "manage"


def visible_to(user_id: uuid.UUID) -> ColumnElement[bool]:
    """Filter for the mailboxes ``user_id`` may read."""
    return Mailbox.owner_user_id == user_id


def permissions(mailbox: Mailbox, user_id: uuid.UUID) -> frozenset[MailboxPermission]:
    if mailbox.owner_user_id == user_id:
        return frozenset(MailboxPermission)
    return frozenset()


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

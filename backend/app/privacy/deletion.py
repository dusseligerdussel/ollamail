"""Deleting a user with all their data (Art. 17).

Every table with personal data hangs off ``users`` (directly or through mailboxes, mails,
conversations, ...) with ``ON DELETE CASCADE``, so deleting the user row removes all rows
in one transaction; ``tests/privacy/test_user_deletion.py`` checks this generically over
all foreign keys. Files are removed after the commit: attachments per mailbox, digest audio
and exports per user. The audit log keeps one ``user.deleted`` entry with IDs and counts
only; the user's name and address disappear from it with the user row.
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.admin_access import AdminAccessGuard
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.mail.models import Mailbox
from app.mail.storage import AttachmentStorage
from app.privacy.storage import ExportStorage
from app.users.models import User, UserRole

log = get_logger(__name__)


@dataclass(frozen=True)
class FileStores:
    attachments: AttachmentStorage
    digests: DigestStorage
    exports: ExportStorage


@dataclass(frozen=True)
class DeletionResult:
    user_id: uuid.UUID
    mailboxes: int


async def _is_last_active_admin(session: AsyncSession, user: User) -> bool:
    if user.role is not UserRole.ADMIN or not user.is_active:
        return False
    # Locks the other admins, so two admins cannot delete each other at the same time.
    other = await session.scalar(
        select(User.id)
        .where(User.role == UserRole.ADMIN, User.is_active, User.id != user.id)
        .limit(1)
        .with_for_update()
    )
    return other is None


def last_admin_error() -> ProblemError:
    return ProblemError(
        409,
        detail="The last active administrator cannot be deleted.",
        type="urn:ollamail:problem:last-admin",
    )


async def delete_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    stores: FileStores,
    *,
    actor: audit.Actor,
    via: str,
    access_guard: AdminAccessGuard | None = None,
) -> DeletionResult | None:
    """Hard-delete a user and everything they own, commit, then remove their files.

    ``via`` (``self``, ``admin``) goes into the audit entry. Returns ``None`` if the user
    does not exist; raises 409 for the last active admin and, with ``access_guard``, if no
    admin who can sign in would be left (``admin-lockout``).
    """
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        return None
    if await _is_last_active_admin(session, user):
        raise last_admin_error()
    mailbox_ids = list(
        await session.scalars(select(Mailbox.id).where(Mailbox.owner_user_id == user_id))
    )
    await session.execute(delete(User).where(User.id == user_id))
    if access_guard is not None:
        await access_guard.check()
    await audit.record(
        session,
        actor,
        audit.AuditAction.USER_DELETED,
        audit.Target.of(audit.TargetType.USER, user_id),
        {"via": via, "mailboxes": len(mailbox_ids)},
    )
    await session.commit()
    session.expunge_all()
    for mailbox_id in mailbox_ids:
        await asyncio.to_thread(stores.attachments.delete_mailbox, mailbox_id)
    await asyncio.to_thread(stores.digests.delete_user, user_id)
    await asyncio.to_thread(stores.exports.delete_user, user_id)
    log.info("privacy_user_deleted", user_id=str(user_id), mailboxes=len(mailbox_ids), via=via)
    return DeletionResult(user_id=user_id, mailboxes=len(mailbox_ids))

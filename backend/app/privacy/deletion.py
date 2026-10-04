"""Deleting a user with all their data (Art. 17).

Every table with personal data hangs off ``users`` (directly or through mailboxes, mails,
conversations, ...) with ``ON DELETE CASCADE``, so deleting the user row removes all rows;
``tests/privacy/test_user_deletion.py`` checks this generically over all foreign keys.
The audit log keeps one ``user.deleted`` entry with IDs and counts only; the user's name
and address disappear from it with the user row.

Mailboxes can hold hundreds of thousands of mails, so a user who owns mailboxes is deleted
in the background (#177), with the same mechanism as a single mailbox
(``app.mail.deletion``, #147):

1. ``delete_user`` (in the request) marks the user (``deletion_requested_at``),
   deactivates and anonymises them and deletes everything that lets anybody sign in as
   them or find them: identities, sessions, invitations, second factors, the SCIM mapping
   and group memberships, assignments to shared mailboxes. Each owned mailbox is marked
   with ``request_deletion`` (``mailbox.deleted`` in the audit log). From the commit on,
   the user and their data are gone for everybody, admins included: no sign-in, not in
   the user list or in SCIM, their mails hidden by ``app.mail.access``.
2. ``mail.delete_mailbox`` removes the mailboxes in batches. ``privacy.delete_user``
   (``app.privacy.tasks``, queued by the request, after each removed mailbox and by the
   periodic ``privacy.resume_user_deletions``) deletes the user row once no mailbox is
   left (cascading to the small rest: todos, conversations, digests, ...), then the
   files: digest audio and exports.

A user without mailboxes is deleted at once, in the request.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.admin_access import AdminAccessGuard
from app.auth.mfa.models import Passkey, PendingLogin, RecoveryCode, TotpFactor
from app.auth.models import AuthSession, Identity, Invitation
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.digest.storage import DigestStorage
from app.mail.deletion import request_deletion
from app.mail.models import Mailbox, MailboxAssignment
from app.notifications.models import PushSubscription
from app.privacy.storage import ExportStorage
from app.scim.models import ScimUser, scim_group_members
from app.users.models import User, UserRole

log = get_logger(__name__)

# Address of a user being deleted: never deliverable (RFC 2606), unique, frees the real
# address at once (a new account or a provider login with it does not find the old user).
ANONYMOUS_DOMAIN = "deleted.invalid"


@dataclass(frozen=True)
class FileStores:
    digests: DigestStorage
    exports: ExportStorage


@dataclass(frozen=True)
class DeletionResult:
    user_id: uuid.UUID
    mailboxes: int
    # The user row is gone already; otherwise ``privacy.delete_user`` finishes the job.
    completed: bool


@dataclass
class PurgeResult:
    # The user row was deleted by this run.
    deleted: bool = False
    # Mailboxes of the user that still have to be removed (by ``mail.delete_mailbox``).
    pending_mailboxes: list[uuid.UUID] = field(default_factory=list)


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


async def _remove_access(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Delete what lets anybody sign in as the user or see them in a list."""
    for model in (
        Identity,
        AuthSession,
        Invitation,
        PendingLogin,
        Passkey,
        TotpFactor,
        RecoveryCode,
        ScimUser,
        MailboxAssignment,
        # Devices that would still be woken by Web Push (#181).
        PushSubscription,
    ):
        await session.execute(delete(model).where(model.user_id == user_id))
    await session.execute(delete(scim_group_members).where(scim_group_members.c.user_id == user_id))


async def _delete_user_files(stores: FileStores, user_id: uuid.UUID) -> None:
    await asyncio.to_thread(stores.digests.delete_user, user_id)
    await asyncio.to_thread(stores.exports.delete_user, user_id)


async def delete_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    stores: FileStores,
    *,
    actor: audit.Actor,
    via: str,
    access_guard: AdminAccessGuard | None = None,
) -> DeletionResult | None:
    """Delete a user and everything they own, or, if they own mailboxes, mark them for
    the background deletion (step 1 above). Commits.

    ``via`` (``self``, ``admin``, ``scim``) goes into the audit entry. Returns ``None`` if
    the user does not exist or is already being deleted; raises 409 for the last active
    admin and, with ``access_guard``, if no admin who can sign in would be left
    (``admin-lockout``). With ``completed=False`` the caller queues ``privacy.delete_user``
    (``app.privacy.tasks.defer_user_deletion``) after the commit.
    """
    user = await session.get(User, user_id, with_for_update=True)
    if user is None or user.deletion_requested_at is not None:
        return None
    if await _is_last_active_admin(session, user):
        raise last_admin_error()
    mailboxes = list(
        await session.scalars(
            select(Mailbox).where(Mailbox.owner_user_id == user_id).with_for_update()
        )
    )
    completed = not mailboxes
    if completed:
        await session.execute(delete(User).where(User.id == user_id))
    else:
        user.deletion_requested_at = datetime.now(UTC)
        user.is_active = False
        user.email = f"{user_id}@{ANONYMOUS_DOMAIN}"
        user.display_name = ""
        await _remove_access(session, user_id)
        for mailbox in mailboxes:
            await request_deletion(session, mailbox, actor)
    if access_guard is not None:
        await access_guard.check()
    await audit.record(
        session,
        actor,
        audit.AuditAction.USER_DELETED,
        audit.Target.of(audit.TargetType.USER, user_id),
        {"via": via, "mailboxes": len(mailboxes)},
    )
    await session.commit()
    session.expunge_all()
    if completed:
        await _delete_user_files(stores, user_id)
        log.info("privacy_user_deleted", user_id=str(user_id), mailboxes=0, via=via)
    else:
        log.info(
            "privacy_user_deletion_requested",
            user_id=str(user_id),
            mailboxes=len(mailboxes),
            via=via,
        )
    return DeletionResult(user_id=user_id, mailboxes=len(mailboxes), completed=completed)


async def purge_user(session: AsyncSession, user_id: uuid.UUID, stores: FileStores) -> PurgeResult:
    """Step 2: delete a user marked by ``delete_user`` once their mailboxes are gone,
    then their files. Commits. Idempotent: a user that is not marked is left alone; one
    that is gone only gets leftover files removed."""
    result = PurgeResult()
    marked = await session.scalar(
        select(User.id).where(User.id == user_id, User.deletion_requested_at.is_not(None))
    )
    if marked is None:
        # Already gone (a previous run finished): remove leftover files only.
        if await session.get(User, user_id) is None:
            await _delete_user_files(stores, user_id)
        return result
    mailboxes = list(await session.scalars(select(Mailbox).where(Mailbox.owner_user_id == user_id)))
    if mailboxes:
        # A mailbox added while the request ran is marked here.
        for mailbox in mailboxes:
            if mailbox.deletion_requested_at is None:
                await request_deletion(session, mailbox, audit.SYSTEM)
        await session.commit()
        result.pending_mailboxes = [mailbox.id for mailbox in mailboxes]
        return result
    deleted = await session.execute(
        delete(User)
        .where(User.id == user_id, User.deletion_requested_at.is_not(None))
        .returning(User.id)
    )
    result.deleted = deleted.first() is not None
    await session.commit()
    await _delete_user_files(stores, user_id)
    log.info("privacy_user_deleted", user_id=str(user_id))
    return result


async def pending_user_deletions(
    session: AsyncSession, user_ids: list[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Users being deleted, oldest request first; only among ``user_ids`` if given."""
    query = select(User.id).where(User.deletion_requested_at.is_not(None))
    if user_ids is not None:
        query = query.where(User.id.in_(user_ids))
    return list(await session.scalars(query.order_by(User.deletion_requested_at)))

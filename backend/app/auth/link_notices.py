"""Tell users when a sign-in was linked to their account by e-mail address (#208).

Linking (``link_by_email`` or SCIM linking, ``app.auth.provisioning``) opens the account to
whoever controls the provider. The audit log records it, but only admins see that, and a
malicious admin is exactly who could abuse it (docs/PRIVACY.md, "Admin ≠ Leser"). So each link
also leaves a notice for the user, shown until the user dismisses it. Stored: user ID, provider
key and time.

Only sessions of a sign-in method the user already had see and dismiss a notice (#220): the
method was linked to the account no later than the notice was created, and it has no open
notice itself. So whoever signs in through a new link cannot hide its notice, nor the notice
of a second provider linked the same way (each new link has its own open notice). The user
confirms a link by dismissing its notice (audit log: ``user.identity_link_confirmed``) or
undoes it by unlinking the provider (``app.auth.identities``), which drops the notice too.
"""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession, Identity, IdentityLinkNotice


async def add_link_notice(db: AsyncSession, user_id: uuid.UUID, provider: str) -> None:
    """Record a new link for ``user_id`` (caller commits)."""
    db.add(IdentityLinkNotice(user_id=user_id, provider=provider))
    await db.flush()


async def _trusted_since(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> datetime | None:
    """When the sign-in method of the session was linked to the account, if the session may
    see link notices; ``None`` if it may not see any.

    It may not if its method has an open notice itself (the user has not confirmed that link)
    or no identity of the account (fail closed)."""
    provider = await db.scalar(
        select(AuthSession.provider).where(
            AuthSession.id == session_id, AuthSession.user_id == user_id
        )
    )
    if provider is None:
        return None
    unconfirmed = await db.scalar(
        select(
            exists().where(
                IdentityLinkNotice.user_id == user_id, IdentityLinkNotice.provider == provider
            )
        )
    )
    if unconfirmed:
        return None
    linked_at: datetime | None = await db.scalar(
        select(func.min(Identity.created_at)).where(
            Identity.user_id == user_id, Identity.provider == provider
        )
    )
    return linked_at


def _visible(user_id: uuid.UUID, since: datetime) -> list[ColumnElement[bool]]:
    # The method of the session has no open notice, so these are other providers' notices.
    return [IdentityLinkNotice.user_id == user_id, IdentityLinkNotice.created_at >= since]


async def list_link_notices(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> list[IdentityLinkNotice]:
    """The user's open notices that the session ``session_id`` may see, newest first."""
    since = await _trusted_since(db, user_id, session_id)
    if since is None:
        return []
    result = await db.scalars(
        select(IdentityLinkNotice)
        .where(*_visible(user_id, since))
        .order_by(IdentityLinkNotice.created_at.desc(), IdentityLinkNotice.id.desc())
    )
    return list(result)


async def dismiss_link_notice(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, notice_id: uuid.UUID
) -> str | None:
    """Delete a notice the session may see; returns its provider (``None``: not found)."""
    since = await _trusted_since(db, user_id, session_id)
    if since is None:
        return None
    result = await db.execute(
        delete(IdentityLinkNotice)
        .where(IdentityLinkNotice.id == notice_id, *_visible(user_id, since))
        .returning(IdentityLinkNotice.provider)
    )
    return result.scalar_one_or_none()

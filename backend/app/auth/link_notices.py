"""Tell users when a sign-in was linked to their account by e-mail address (#208).

Linking (``link_by_email`` or SCIM linking, ``app.auth.provisioning``) opens the account to
whoever controls the provider. The audit log records it, but only admins see that, and a
malicious admin is exactly who could abuse it (docs/PRIVACY.md, "Admin ≠ Leser"). So each link
also leaves a notice for the user: it is shown in sessions of *other* sign-in methods until the
user dismisses it. Sessions of the linked provider neither see nor dismiss it, so whoever
signs in through the new link cannot hide it. Stored: user ID, provider key and time.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession, IdentityLinkNotice


async def add_link_notice(db: AsyncSession, user_id: uuid.UUID, provider: str) -> None:
    """Record a new link for ``user_id`` (caller commits)."""
    db.add(IdentityLinkNotice(user_id=user_id, provider=provider))
    await db.flush()


async def _session_provider(db: AsyncSession, session_id: uuid.UUID) -> str | None:
    return await db.scalar(select(AuthSession.provider).where(AuthSession.id == session_id))


async def list_link_notices(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> list[IdentityLinkNotice]:
    """The user's open notices that the session ``session_id`` may see, newest first."""
    provider = await _session_provider(db, session_id)
    if provider is None:
        return []
    result = await db.scalars(
        select(IdentityLinkNotice)
        .where(IdentityLinkNotice.user_id == user_id, IdentityLinkNotice.provider != provider)
        .order_by(IdentityLinkNotice.created_at.desc(), IdentityLinkNotice.id.desc())
    )
    return list(result)


async def dismiss_link_notice(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, notice_id: uuid.UUID
) -> str | None:
    """Delete a notice the session may see; returns its provider (``None``: not found)."""
    provider = await _session_provider(db, session_id)
    if provider is None:
        return None
    result = await db.execute(
        delete(IdentityLinkNotice)
        .where(
            IdentityLinkNotice.id == notice_id,
            IdentityLinkNotice.user_id == user_id,
            IdentityLinkNotice.provider != provider,
        )
        .returning(IdentityLinkNotice.provider)
    )
    return result.scalar_one_or_none()

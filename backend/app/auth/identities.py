"""The own sign-in methods: list them, unlink external ones and block their relinking (#216).

A sign-in linked to an account by e-mail address (``link_by_email`` or SCIM linking,
``app.auth.provisioning``) opens the account to whoever controls the provider; #208 tells
the user about it (``app.auth.link_notices``). Here the user can undo it without an admin,
who may be the one abusing it (docs/PRIVACY.md, "Admin ≠ Leser"):

* Unlinking deletes the identity, ends the sessions of its provider, drops the open link
  notices about it and blocks the provider: ``provision_user`` no longer links it to the
  account by e-mail address (``auth_identity_link_blocks``). Otherwise the next sign-in
  through the provider would link it again at once.
* Refused (409) for ``local`` and ``scim``, for the provider of the current session (the
  user would cut the branch they sit on, and whoever controls a hostile provider must not
  remove the user's own sign-in) and for the last way to sign in. Password and passkeys
  count as local sign-in.
* Only the user lifts a block; there is deliberately no admin endpoint for it. Sessions of
  the blocked provider neither see nor lift it.
"""

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.mfa.models import Passkey
from app.auth.models import (
    LOCAL_PROVIDER,
    SCIM_PROVIDER,
    AuthSession,
    Identity,
    IdentityLinkBlock,
    IdentityLinkNotice,
)
from app.core.ids import uuid7


class UnlinkRefusal(enum.StrEnum):
    # Local sign-in (password, passkeys) is managed in the security settings.
    LOCAL = "local"
    # The current session signed in with this provider.
    CURRENT_SESSION = "current_session"
    # Without it the account had no way to sign in left.
    LAST_SIGN_IN = "last_sign_in"


@dataclass(frozen=True)
class SignInMethod:
    identity: Identity
    # The current session signed in with this provider.
    current: bool
    # Why it cannot be unlinked (``None``: it can).
    refusal: UnlinkRefusal | None


@dataclass(frozen=True)
class Unlinked:
    provider: str
    sessions: int


async def session_provider(db: AsyncSession, session_id: uuid.UUID) -> str | None:
    return await db.scalar(select(AuthSession.provider).where(AuthSession.id == session_id))


async def _has_passkey(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return bool(await db.scalar(select(exists().where(Passkey.user_id == user_id))))


def _signs_in(identity: Identity, has_passkey: bool) -> bool:
    """Whether the identity is a way to sign in (a local one without password and passkey is
    a pending invitation)."""
    if identity.provider == LOCAL_PROVIDER:
        return identity.password_hash is not None or has_passkey
    return identity.provider != SCIM_PROVIDER


async def list_sign_in_methods(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> list[SignInMethod]:
    """The user's ways to sign in (without SCIM), oldest first, each with whether it can be
    unlinked from the session ``session_id``."""
    current = await session_provider(db, session_id)
    has_passkey = await _has_passkey(db, user_id)
    rows = await db.scalars(
        select(Identity)
        .where(Identity.user_id == user_id)
        .order_by(Identity.created_at, Identity.id)
    )
    identities = [row for row in rows if _signs_in(row, has_passkey)]
    return [
        SignInMethod(
            identity, identity.provider == current, _refusal(identity, identities, current)
        )
        for identity in identities
    ]


def _refusal(
    identity: Identity, identities: list[Identity], current: str | None
) -> UnlinkRefusal | None:
    if identity.provider == LOCAL_PROVIDER:
        return UnlinkRefusal.LOCAL
    if identity.provider == current:
        return UnlinkRefusal.CURRENT_SESSION
    if all(other.id == identity.id for other in identities):
        return UnlinkRefusal.LAST_SIGN_IN
    return None


async def find_sign_in_method(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, identity_id: uuid.UUID
) -> SignInMethod | None:
    """One of the user's ways to sign in; ``None`` if there is no such (or not theirs)."""
    methods = await list_sign_in_methods(db, user_id, session_id)
    return next((method for method in methods if method.identity.id == identity_id), None)


async def unlink(db: AsyncSession, user_id: uuid.UUID, identity: Identity) -> Unlinked:
    """Delete an unlinkable identity with its sessions and link notices and block its provider
    from linking again (caller checks ``SignInMethod.refusal``, audits and commits)."""
    provider = identity.provider
    await db.delete(identity)
    sessions = await db.execute(
        delete(AuthSession)
        .where(AuthSession.user_id == user_id, AuthSession.provider == provider)
        .returning(AuthSession.id)
    )
    await db.execute(
        delete(IdentityLinkNotice).where(
            IdentityLinkNotice.user_id == user_id, IdentityLinkNotice.provider == provider
        )
    )
    await db.execute(
        insert(IdentityLinkBlock)
        .values(id=uuid7(), user_id=user_id, provider=provider)
        .on_conflict_do_nothing(index_elements=["user_id", "provider"])
    )
    await db.flush()
    return Unlinked(provider=provider, sessions=len(sessions.all()))


async def link_blocked(db: AsyncSession, user_id: uuid.UUID, provider: str) -> bool:
    """Whether the user blocked ``provider`` from linking to their account by e-mail address."""
    return bool(
        await db.scalar(
            select(
                exists().where(
                    IdentityLinkBlock.user_id == user_id, IdentityLinkBlock.provider == provider
                )
            )
        )
    )


async def list_link_blocks(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID
) -> list[IdentityLinkBlock]:
    """The user's blocked providers that the session ``session_id`` may see, newest first."""
    provider = await session_provider(db, session_id)
    if provider is None:
        return []
    result = await db.scalars(
        select(IdentityLinkBlock)
        .where(IdentityLinkBlock.user_id == user_id, IdentityLinkBlock.provider != provider)
        .order_by(IdentityLinkBlock.created_at.desc(), IdentityLinkBlock.id.desc())
    )
    return list(result)


async def lift_link_block(
    db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID, block_id: uuid.UUID
) -> str | None:
    """Delete a block the session may see; returns its provider (``None``: not found)."""
    provider = await session_provider(db, session_id)
    if provider is None:
        return None
    result = await db.execute(
        delete(IdentityLinkBlock)
        .where(
            IdentityLinkBlock.id == block_id,
            IdentityLinkBlock.user_id == user_id,
            IdentityLinkBlock.provider != provider,
        )
        .returning(IdentityLinkBlock.provider)
    )
    return result.scalar_one_or_none()

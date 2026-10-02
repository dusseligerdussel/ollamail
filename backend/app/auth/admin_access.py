"""Protection against locking every admin out (#33).

An admin has *working access* if the account is active and has an identity at a provider
that currently accepts logins: a local password while local login is on, an enabled OIDC
provider (also GitHub and other registry providers) or an enabled LDAP directory.

Every admin change that can remove access (role, deactivation, local login switch,
disabling or deleting a provider) is checked server-side with ``AdminAccessGuard``: the
change is rejected (409, ``admin-lockout``) if it would leave no admin with working access.
The emergency CLI (``python -m app.cli reset-password`` / ``create-admin``) is the way back
if access is lost anyway, e.g. because the IdP itself is down.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import LOCAL_PROVIDER, Identity
from app.auth.policy import local_login_enabled
from app.auth.providers.base import AuthProviderRegistry
from app.auth.providers.ldap.service import list_directories
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User, UserRole

log = get_logger(__name__)

LOCKOUT_TYPE = "urn:ollamail:problem:admin-lockout"


async def enabled_providers(db: AsyncSession, registry: AuthProviderRegistry) -> set[str]:
    """Provider keys that accept logins right now."""
    names = {provider.name for provider in await registry.available(db)}
    names |= {directory.provider for directory in await list_directories(db, enabled_only=True)}
    if await local_login_enabled(db):
        names.add(LOCAL_PROVIDER)
    return names


async def admin_access(
    db: AsyncSession, registry: AuthProviderRegistry
) -> dict[uuid.UUID, set[str]]:
    """Active admins with working access and the providers they can sign in with."""
    providers = await enabled_providers(db, registry)
    rows = await db.execute(
        select(User.id, Identity.provider)
        .join(Identity, Identity.user_id == User.id)
        .where(
            User.role == UserRole.ADMIN,
            User.is_active,
            Identity.provider.in_(providers),
            # A local identity without password is a pending invitation.
            or_(Identity.provider != LOCAL_PROVIDER, Identity.password_hash.is_not(None)),
        )
    )
    access: dict[uuid.UUID, set[str]] = {}
    for user_id, provider in rows:
        access.setdefault(user_id, set()).add(provider)
    return access


def lockout_error() -> ProblemError:
    return ProblemError(
        409,
        detail="This change would leave no administrator who can sign in.",
        type=LOCKOUT_TYPE,
    )


class AdminAccessGuard:
    """Snapshot before a change, ``check`` after it (flushed, not committed)::

        guard = await AdminAccessGuard.start(db, registry)
        user.is_active = False
        await guard.check()  # rolls back and raises 409 if no admin access is left
        await db.commit()

    A change is only rejected if it removes the last working access; if there was none
    before, the admin can still repair the configuration step by step.
    """

    def __init__(self, db: AsyncSession, registry: AuthProviderRegistry, before: int) -> None:
        self._db = db
        self._registry = registry
        self.before = before

    @classmethod
    async def start(cls, db: AsyncSession, registry: AuthProviderRegistry) -> "AdminAccessGuard":
        return cls(db, registry, len(await admin_access(db, registry)))

    async def check(self) -> None:
        await self._db.flush()
        after = len(await admin_access(self._db, self._registry))
        if after == 0 and self.before > 0:
            await self._db.rollback()
            log.warning("admin_lockout_prevented")
            raise lockout_error()


async def other_active_admins(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Whether another active admin exists (used at login, where providers are not known)."""
    other = await db.scalar(
        select(User.id)
        .where(User.role == UserRole.ADMIN, User.is_active, User.id != user_id)
        .limit(1)
    )
    return other is not None

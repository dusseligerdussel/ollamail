"""Just-in-time provisioning: the user for an identity verified by an external provider.

Shared by all external providers (LDAP #32; OIDC #30 and GitHub #31 can use it as well).
On the first login a user is created from the identity's e-mail address and display
name; later logins find it through ``auth_identities``. An existing account with the
same e-mail address is *not* linked automatically: whoever controls an e-mail attribute
in an external directory could otherwise take over a local (admin) account.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Identity
from app.auth.providers import VerifiedIdentity
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User, UserRole
from app.users.schemas import normalize_email
from app.users.service import get_user_by_email

log = get_logger(__name__)


def account_exists() -> ProblemError:
    return ProblemError(
        409,
        detail=(
            "An account with this e-mail address already exists. Ask an administrator to link it."
        ),
        type="urn:ollamail:problem:account-exists",
    )


def missing_email() -> ProblemError:
    return ProblemError(
        403,
        detail="The identity provider did not supply an e-mail address.",
        type="urn:ollamail:problem:missing-email",
    )


async def provision_user(
    db: AsyncSession, identity: VerifiedIdentity, *, role: UserRole | None = None
) -> User | None:
    """The active user linked to ``identity``, created on first login (caller commits).

    ``role`` is the role derived from the provider's group mapping; ``None`` means the
    provider does not manage roles (new users get ``user``, existing ones keep theirs).
    Returns ``None`` for deactivated users. Raises ``ProblemError`` 403 without e-mail
    address and 409 if the address belongs to another account.
    """
    row = (
        await db.execute(
            select(Identity, User)
            .join(User, User.id == Identity.user_id)
            .where(Identity.provider == identity.provider, Identity.subject == identity.subject)
        )
    ).one_or_none()
    now = datetime.now(UTC)
    if row is not None:
        linked, user = row
        if not user.is_active:
            return None
        linked.last_used_at = now
        if role is not None and user.role != role:
            log.info("user_role_synced", user_id=user.id, role=role, provider=identity.provider)
            user.role = role
        return user

    try:
        email = normalize_email(identity.email or "")
    except ValueError:
        raise missing_email() from None
    if await get_user_by_email(db, email) is not None:
        raise account_exists()
    user = User(
        email=email,
        display_name=(identity.display_name or email.partition("@")[0])[:255],
        role=role or UserRole.USER,
        is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
        db.add(
            Identity(
                user_id=user.id,
                provider=identity.provider,
                subject=identity.subject,
                last_used_at=now,
            )
        )
        await db.flush()
    except IntegrityError:
        # A parallel first login of the same person (or the same address) won.
        await db.rollback()
        raise account_exists() from None
    await db.refresh(user)
    log.info("user_provisioned", user_id=user.id, role=user.role, provider=identity.provider)
    return user

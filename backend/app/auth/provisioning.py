"""Just-in-time provisioning: map an external identity to a user, creating it if allowed.

Shared by all external providers (OIDC #30, GitHub #31, LDAP #32). The provider proves the
identity; this module decides whether that identity may use the instance:

1. A known identity (``provider``, ``subject``) signs in as its user. Its groups are
   updated on every login, so the role mapping (#33) always sees the current state.
2. An unknown identity needs an e-mail address. If a user with that address exists, the
   identity is linked to it only if the provider allows linking *and* vouches for the
   address (``email_verified``); otherwise the login is refused. Linking on an unverified
   address would let anyone who can set an e-mail address at the IdP take over accounts.
3. Otherwise a new user (role ``user``) is created if the provider allows provisioning.

The domain allowlist is checked on every login (also for known identities, so narrowing
it takes effect immediately) and only accepts verified addresses.

The role is resolved centrally on every login (``app.auth.policy.resolve_role``, #33): with
the group → role mapping switched on, the admin's rules and default role decide, for all
providers alike; a role the provider derives itself (``role``, LDAP ``admin_groups``) counts
as a matching rule. With the mapping off, ``role`` is applied as is; without one new users
get ``user`` and existing users keep their role. The last active admin is never demoted at
login (the change is skipped and logged), so a wrong rule cannot lock the instance out.
Creating a user, linking a login to an existing account by e-mail address and changing a
role are recorded in the audit log (actor ``system``); a link also leaves a notice for the
user (``app.auth.link_notices``, #208).

Users created by SCIM (#95): the groups SCIM keeps for them count for the role mapping
like the groups of the login, and providers the admin lists in the SCIM settings may link
a login to them by verified e-mail address without ``link_by_email``.

``ProvisioningError`` is a ``ProblemError``: JSON endpoints (password providers) can let it
propagate, browser flows (``app.auth.redirect_flow``) use its static ``code``.
"""

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.admin_access import other_active_admins
from app.auth.link_notices import add_link_notice
from app.auth.models import SCIM_PROVIDER, Identity
from app.auth.policy import resolve_role
from app.auth.providers.base import VerifiedIdentity
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.scim.models import ScimConfig, ScimUser
from app.users.models import User, UserRole
from app.users.schemas import normalize_email
from app.users.service import get_user_by_email

log = get_logger(__name__)

# Stored groups per identity; IdPs can report hundreds (Entra caps the claim at 200).
MAX_GROUPS = 500
_MAX_GROUP_LENGTH = 255
_MAX_DISPLAY_NAME = 255


class ProvisioningErrorCode(enum.StrEnum):
    # The account exists but is deactivated.
    INACTIVE = "inactive"
    # The provider sent no usable e-mail address.
    EMAIL_MISSING = "email_missing"
    # The e-mail domain is not on the allowlist (or the address is not verified).
    DOMAIN_NOT_ALLOWED = "domain_not_allowed"
    # Another user has this e-mail address and linking is not allowed.
    EMAIL_CONFLICT = "email_conflict"
    # Unknown user and just-in-time provisioning is off.
    NOT_PROVISIONED = "not_provisioned"


# Problem types used by the JSON login endpoints (LDAP) since #32; kept stable.
_TYPES = {
    ProvisioningErrorCode.EMAIL_CONFLICT: "urn:ollamail:problem:account-exists",
    ProvisioningErrorCode.EMAIL_MISSING: "urn:ollamail:problem:missing-email",
}

_ERRORS: dict[ProvisioningErrorCode, tuple[int, str]] = {
    ProvisioningErrorCode.INACTIVE: (403, "The account is deactivated."),
    ProvisioningErrorCode.EMAIL_MISSING: (
        403,
        "The identity provider did not supply an e-mail address.",
    ),
    ProvisioningErrorCode.DOMAIN_NOT_ALLOWED: (
        403,
        "Sign-in with this e-mail domain is not allowed.",
    ),
    ProvisioningErrorCode.EMAIL_CONFLICT: (
        409,
        "An account with this e-mail address already exists. Ask an administrator to link it.",
    ),
    ProvisioningErrorCode.NOT_PROVISIONED: (
        403,
        "There is no account for this identity. Ask an administrator for access.",
    ),
}


class ProvisioningError(ProblemError):
    """The identity may not sign in; ``code`` is static, safe to show and to log."""

    def __init__(self, code: ProvisioningErrorCode) -> None:
        status, detail = _ERRORS[code]
        super().__init__(
            status,
            detail=detail,
            type=_TYPES.get(code, f"urn:ollamail:problem:{code.value.replace('_', '-')}"),
        )
        self.code = code


@dataclass(frozen=True)
class ProvisioningPolicy:
    """Per-provider rules (admin settings)."""

    # Create unknown users on their first login.
    auto_provision: bool = True
    # Link an unknown identity to an existing user with the same verified e-mail address.
    link_by_email: bool = False
    # Lower-case e-mail domains allowed to sign in; empty allows all.
    allowed_domains: frozenset[str] = field(default_factory=frozenset)
    # Store the identity's groups for the role mapping (#33) and the group assignments of
    # shared mailboxes (#34). Off for providers whose groups must not be kept.
    store_groups: bool = True


@dataclass(frozen=True)
class ProvisioningResult:
    user: User
    created: bool = False
    linked: bool = False


def normalize_domains(domains: Iterable[str]) -> frozenset[str]:
    return frozenset(d.strip().lower().lstrip("@") for d in domains if d.strip())


def clean_groups(groups: Iterable[object]) -> list[str]:
    """Distinct, sorted, non-empty group strings within the storage limits."""
    cleaned = {str(g).strip()[:_MAX_GROUP_LENGTH] for g in groups if str(g).strip()}
    return sorted(cleaned)[:MAX_GROUPS]


def _email(identity: VerifiedIdentity) -> str | None:
    if not identity.email:
        return None
    try:
        return normalize_email(identity.email)
    except ValueError:
        return None


def _check_domain(
    policy: ProvisioningPolicy, identity: VerifiedIdentity, email: str | None
) -> None:
    if not policy.allowed_domains:
        return
    if email is None or not identity.email_verified:
        raise ProvisioningError(ProvisioningErrorCode.DOMAIN_NOT_ALLOWED)
    if email.rpartition("@")[2] not in policy.allowed_domains:
        raise ProvisioningError(ProvisioningErrorCode.DOMAIN_NOT_ALLOWED)


def _display_name(identity: VerifiedIdentity, email: str) -> str:
    name = (identity.display_name or "").strip()
    return (name or email.partition("@")[0])[:_MAX_DISPLAY_NAME]


def _touch(identity_row: Identity, identity: VerifiedIdentity, policy: ProvisioningPolicy) -> None:
    identity_row.groups = clean_groups(identity.groups) if policy.store_groups else []
    identity_row.last_used_at = datetime.now(UTC)


async def _known(db: AsyncSession, identity: VerifiedIdentity) -> tuple[Identity, User] | None:
    row = (
        await db.execute(
            select(Identity, User)
            .join(User, User.id == Identity.user_id)
            .where(Identity.provider == identity.provider, Identity.subject == identity.subject)
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


async def provision_user(
    db: AsyncSession,
    identity: VerifiedIdentity,
    policy: ProvisioningPolicy | None = None,
    *,
    role: UserRole | None = None,
) -> ProvisioningResult:
    """The user for ``identity``; links or creates one if the policy allows it.

    ``role`` is the role from the provider's group mapping (``None``: not managed by the
    provider). Flushes but does not commit. Raises ``ProvisioningError`` if the login is
    refused.
    """
    policy = policy or ProvisioningPolicy()
    email = _email(identity)
    _check_domain(policy, identity, email)
    provider_role = role

    known = await _known(db, identity)
    if known is not None:
        identity_row, user = known
        if not user.is_active:
            raise ProvisioningError(ProvisioningErrorCode.INACTIVE)
        _touch(identity_row, identity, policy)
        role = await _resolve_role(db, identity, provider_role, user)
        await _sync_role(db, user, identity, role)
        await db.flush()
        return ProvisioningResult(user=user)

    if email is None:
        raise ProvisioningError(ProvisioningErrorCode.EMAIL_MISSING)

    existing = await get_user_by_email(db, email)
    if existing is not None:
        may_link = policy.link_by_email or await _scim_link_allowed(db, existing, identity)
        if not (may_link and identity.email_verified):
            raise ProvisioningError(ProvisioningErrorCode.EMAIL_CONFLICT)
        if not existing.is_active:
            raise ProvisioningError(ProvisioningErrorCode.INACTIVE)
        role = await _resolve_role(db, identity, provider_role, existing)
        await _sync_role(db, existing, identity, role)
        await _add_identity(db, existing, identity, policy)
        # Linking hands the account to whoever controls the provider (#190): keep it visible
        # in the audit log.
        await audit.record(
            db,
            audit.SYSTEM,
            audit.AuditAction.USER_IDENTITY_LINKED,
            audit.Target.of(audit.TargetType.USER, existing.id),
            {"provider": identity.provider, "via": "email" if policy.link_by_email else "scim"},
        )
        # ... and tell the user in their other sign-ins (#208).
        await add_link_notice(db, existing.id, identity.provider)
        log.info("identity_linked", user_id=existing.id, provider=identity.provider)
        return ProvisioningResult(user=existing, linked=True)

    if not policy.auto_provision:
        raise ProvisioningError(ProvisioningErrorCode.NOT_PROVISIONED)
    role = await _resolve_role(db, identity, provider_role, None)
    return await _create(db, identity, email, role or UserRole.USER, policy)


async def _resolve_role(
    db: AsyncSession, identity: VerifiedIdentity, provider_role: UserRole | None, user: User | None
) -> UserRole | None:
    """``resolve_role`` with the groups SCIM (#95) keeps for ``user`` counted as well."""
    directory_groups: list[tuple[str, list[str]]] = []
    if user is not None:
        scim_groups = await db.scalar(
            select(Identity.groups).where(
                Identity.user_id == user.id, Identity.provider == SCIM_PROVIDER
            )
        )
        if scim_groups:
            directory_groups.append((SCIM_PROVIDER, scim_groups))
    return await resolve_role(
        db, identity.provider, identity.groups, provider_role, directory_groups
    )


async def _scim_link_allowed(db: AsyncSession, user: User, identity: VerifiedIdentity) -> bool:
    """Whether the admin allows ``identity.provider`` to sign in users SCIM created (#95)."""
    allowed = await db.scalar(select(ScimConfig.link_providers))
    if not allowed or identity.provider not in allowed:
        return False
    scim_user = await db.scalar(select(ScimUser.id).where(ScimUser.user_id == user.id))
    return scim_user is not None


async def _sync_role(
    db: AsyncSession, user: User, identity: VerifiedIdentity, role: UserRole | None
) -> None:
    if role is None or user.role == role:
        return
    if user.role is UserRole.ADMIN and not await other_active_admins(db, user.id):
        log.warning("user_role_sync_skipped", user_id=user.id, reason="last_admin")
        return
    log.info("user_role_synced", user_id=user.id, role=role, provider=identity.provider)
    await audit.record(
        db,
        audit.SYSTEM,
        audit.AuditAction.USER_ROLE_CHANGED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"from_role": str(user.role), "to_role": str(role), "provider": identity.provider},
    )
    user.role = role


async def _add_identity(
    db: AsyncSession, user: User, identity: VerifiedIdentity, policy: ProvisioningPolicy
) -> Identity:
    row = Identity(user_id=user.id, provider=identity.provider, subject=identity.subject)
    _touch(row, identity, policy)
    db.add(row)
    await db.flush()
    return row


async def _create(
    db: AsyncSession,
    identity: VerifiedIdentity,
    email: str,
    role: UserRole,
    policy: ProvisioningPolicy,
) -> ProvisioningResult:
    try:
        async with db.begin_nested():
            user = User(
                email=email,
                display_name=_display_name(identity, email),
                role=role,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            await _add_identity(db, user, identity, policy)
    except IntegrityError:
        # A concurrent first login of the same person (or the same address) won the race.
        known = await _known(db, identity)
        if known is not None and known[1].is_active:
            return ProvisioningResult(user=known[1])
        raise ProvisioningError(ProvisioningErrorCode.EMAIL_CONFLICT) from None
    await db.refresh(user)
    await audit.record(
        db,
        audit.SYSTEM,
        audit.AuditAction.USER_CREATED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"role": str(user.role), "provider": identity.provider},
    )
    log.info("user_provisioned", user_id=user.id, role=user.role, provider=identity.provider)
    return ProvisioningResult(user=user, created=True)

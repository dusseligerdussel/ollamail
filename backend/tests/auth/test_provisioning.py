"""Just-in-time provisioning shared by external providers (OIDC, GitHub, LDAP)."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import Identity
from app.auth.providers.base import VerifiedIdentity
from app.auth.provisioning import (
    MAX_GROUPS,
    ProvisioningError,
    ProvisioningErrorCode,
    ProvisioningPolicy,
    clean_groups,
    normalize_domains,
    provision_user,
)
from app.core.errors import ProblemError
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import make_local_user

pytestmark = pytest.mark.db


def _identity(**overrides: object) -> VerifiedIdentity:
    values: dict[str, object] = {
        "provider": "oidc:corp",
        "subject": "sub-1",
        "email": "Erika@Example.org",
        "display_name": "Erika Mustermann",
        "groups": frozenset({"staff"}),
        "email_verified": True,
    }
    values.update(overrides)
    return VerifiedIdentity(**values)  # type: ignore[arg-type]


async def _count(db: AsyncSession, model: type[object]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def test_creates_user_with_role_user(db_session: AsyncSession) -> None:
    result = await provision_user(db_session, _identity(), ProvisioningPolicy())

    assert result.created
    assert result.user.email == "erika@example.org"
    assert result.user.display_name == "Erika Mustermann"
    assert result.user.role is UserRole.USER
    identity = await db_session.scalar(select(Identity))
    assert identity is not None
    assert (identity.provider, identity.subject, identity.groups) == (
        "oidc:corp",
        "sub-1",
        ["staff"],
    )


async def test_display_name_falls_back_to_local_part(db_session: AsyncSession) -> None:
    result = await provision_user(db_session, _identity(display_name=None), ProvisioningPolicy())

    assert result.user.display_name == "erika"


async def test_known_identity_signs_in_and_updates_groups(db_session: AsyncSession) -> None:
    first = await provision_user(db_session, _identity(), ProvisioningPolicy())

    again = await provision_user(
        db_session,
        _identity(email="other@example.org", groups=frozenset({"admins", "staff"})),
        ProvisioningPolicy(auto_provision=False),
    )

    assert again.user.id == first.user.id
    assert not again.created
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.groups == ["admins", "staff"]
    assert await _count(db_session, User) == 1


async def test_auto_provision_off_refuses_unknown_users(db_session: AsyncSession) -> None:
    with pytest.raises(ProvisioningError) as exc:
        await provision_user(db_session, _identity(), ProvisioningPolicy(auto_provision=False))

    assert exc.value.code is ProvisioningErrorCode.NOT_PROVISIONED
    assert await _count(db_session, User) == 0


async def test_missing_email_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(ProvisioningError) as exc:
        await provision_user(db_session, _identity(email=None), ProvisioningPolicy())

    assert exc.value.code is ProvisioningErrorCode.EMAIL_MISSING


async def test_existing_email_is_not_linked_by_default(db_session: AsyncSession) -> None:
    await make_local_user(db_session)

    with pytest.raises(ProvisioningError) as exc:
        await provision_user(db_session, _identity(), ProvisioningPolicy())

    assert exc.value.code is ProvisioningErrorCode.EMAIL_CONFLICT
    assert await _count(db_session, Identity) == 1


async def test_linking_requires_verified_email(db_session: AsyncSession) -> None:
    """Otherwise anyone who can set an address at the IdP could take over accounts."""
    await make_local_user(db_session)

    with pytest.raises(ProvisioningError) as exc:
        await provision_user(
            db_session, _identity(email_verified=False), ProvisioningPolicy(link_by_email=True)
        )

    assert exc.value.code is ProvisioningErrorCode.EMAIL_CONFLICT


async def test_verified_email_links_when_allowed(db_session: AsyncSession) -> None:
    user = await make_local_user(db_session, role=UserRole.ADMIN)

    result = await provision_user(db_session, _identity(), ProvisioningPolicy(link_by_email=True))

    assert result.linked
    assert result.user.id == user.id
    assert result.user.role is UserRole.ADMIN
    providers = set(await db_session.scalars(select(Identity.provider)))
    assert providers == {"local", "oidc:corp"}
    # The link is in the audit log (#190): the account now opens to that provider.
    (event,) = await audit_rows(db_session, AuditAction.USER_IDENTITY_LINKED)
    assert event.target_id == str(user.id)
    assert event.actor_kind == "system"
    assert event.details == {"provider": "oidc:corp", "via": "email"}


async def test_inactive_users_are_refused(db_session: AsyncSession) -> None:
    result = await provision_user(db_session, _identity(), ProvisioningPolicy())
    result.user.is_active = False
    await db_session.flush()

    with pytest.raises(ProvisioningError) as exc:
        await provision_user(db_session, _identity(), ProvisioningPolicy())

    assert exc.value.code is ProvisioningErrorCode.INACTIVE


@pytest.mark.parametrize(
    ("email", "verified", "allowed"),
    [
        ("erika@example.org", True, True),
        ("erika@EXAMPLE.org", True, True),
        ("erika@sub.example.org", True, False),
        ("erika@evil.example", True, False),
        ("erika@example.org", False, False),
        (None, True, False),
    ],
)
async def test_domain_allowlist(
    db_session: AsyncSession, email: str | None, verified: bool, allowed: bool
) -> None:
    policy = ProvisioningPolicy(allowed_domains=normalize_domains(["@Example.org "]))
    identity = _identity(email=email, email_verified=verified)

    if allowed:
        assert (await provision_user(db_session, identity, policy)).created
        return
    with pytest.raises(ProvisioningError) as exc:
        await provision_user(db_session, identity, policy)
    assert exc.value.code is ProvisioningErrorCode.DOMAIN_NOT_ALLOWED


async def test_allowlist_applies_to_known_identities(db_session: AsyncSession) -> None:
    await provision_user(db_session, _identity(), ProvisioningPolicy())

    with pytest.raises(ProvisioningError) as exc:
        await provision_user(
            db_session,
            _identity(),
            ProvisioningPolicy(allowed_domains=frozenset({"other.example"})),
        )

    assert exc.value.code is ProvisioningErrorCode.DOMAIN_NOT_ALLOWED


def test_clean_groups_limits_and_sorts() -> None:
    groups = [" b ", "a", "", "a", 42, "x" * 300, *[f"g{i:04}" for i in range(MAX_GROUPS)]]

    cleaned = clean_groups(groups)

    assert len(cleaned) == MAX_GROUPS
    assert cleaned[:3] == ["42", "a", "b"]
    assert all(len(g) <= 255 for g in cleaned)


async def test_role_from_group_mapping_is_applied_on_every_login(
    db_session: AsyncSession,
) -> None:
    created = await provision_user(db_session, _identity(), role=UserRole.ADMIN)
    assert created.user.role is UserRole.ADMIN

    unchanged = await provision_user(db_session, _identity())
    assert unchanged.user.role is UserRole.ADMIN

    await make_local_user(db_session, "root@example.org", role=UserRole.ADMIN)
    demoted = await provision_user(db_session, _identity(), role=UserRole.USER)
    assert demoted.user.role is UserRole.USER


async def test_last_active_admin_is_not_demoted_at_login(db_session: AsyncSession) -> None:
    await provision_user(db_session, _identity(), role=UserRole.ADMIN)

    kept = await provision_user(db_session, _identity(), role=UserRole.USER)

    assert kept.user.role is UserRole.ADMIN
    assert await audit_rows(db_session, AuditAction.USER_ROLE_CHANGED) == []


def test_errors_are_problem_details() -> None:
    conflict = ProvisioningError(ProvisioningErrorCode.EMAIL_CONFLICT)
    missing = ProvisioningError(ProvisioningErrorCode.EMAIL_MISSING)

    assert isinstance(conflict, ProblemError)
    # Problem types of the JSON login endpoints (LDAP, #32) stay stable.
    assert (conflict.status, conflict.type) == (409, "urn:ollamail:problem:account-exists")
    assert (missing.status, missing.type) == (403, "urn:ollamail:problem:missing-email")


async def test_creation_and_role_changes_are_audited(db_session: AsyncSession) -> None:
    created = await provision_user(db_session, _identity())
    await provision_user(db_session, _identity(), role=UserRole.ADMIN)

    (creation,) = await audit_rows(db_session, AuditAction.USER_CREATED)
    (change,) = await audit_rows(db_session, AuditAction.USER_ROLE_CHANGED)
    assert creation.target_id == str(created.user.id)
    assert creation.details == {"role": "user", "provider": "oidc:corp"}
    assert change.details == {"from_role": "user", "to_role": "admin", "provider": "oidc:corp"}


async def test_groups_are_not_stored_when_the_provider_opts_out(db_session: AsyncSession) -> None:
    await provision_user(db_session, _identity(), ProvisioningPolicy(store_groups=False))

    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.groups == []

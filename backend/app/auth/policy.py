"""Instance-wide sign-in policy: local login switch and the group → role mapping (#33).

The mapping is evaluated centrally for every external login in
``app.auth.provisioning.provision_user`` (OIDC, LDAP, GitHub alike):

* Mapping off: the role is whatever the provider supplies (LDAP ``admin_groups``) or,
  without one, unchanged (new users: ``user``). Admins manage roles in the user list.
* Mapping on: the highest role of all rules matching one of the identity's groups wins,
  together with a role the provider supplies itself; without any match the user gets the
  default role. Group names compare case-insensitively (LDAP DNs, Entra object IDs).
"""

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthPolicy, RoleMappingRule
from app.users.models import UserRole

# Higher wins when several rules match.
ROLE_RANK = {UserRole.USER: 0, UserRole.ADMIN: 1}


@dataclass(frozen=True)
class Rule:
    group: str
    provider: str | None
    role: UserRole

    def matches(self, provider: str, groups: frozenset[str]) -> bool:
        if self.provider is not None and self.provider != provider:
            return False
        return self.group.casefold() in groups


async def get_policy(db: AsyncSession) -> AuthPolicy:
    """The stored policy, or an unsaved one with the defaults."""
    policy = await db.scalar(select(AuthPolicy))
    if policy is None:
        policy = AuthPolicy(local_login_enabled=True, role_mapping_enabled=False)
        policy.default_role = UserRole.USER
    return policy


async def get_policy_for_update(db: AsyncSession) -> AuthPolicy:
    """The policy row, locked for the transaction; created with defaults if missing."""
    policy = await db.scalar(select(AuthPolicy).with_for_update())
    if policy is None:
        policy = await get_policy(db)
        db.add(policy)
        await db.flush()
    return policy


async def local_login_enabled(db: AsyncSession) -> bool:
    enabled = await db.scalar(select(AuthPolicy.local_login_enabled))
    return True if enabled is None else enabled


async def list_rules(db: AsyncSession) -> list[RoleMappingRule]:
    return list(
        await db.scalars(
            select(RoleMappingRule).order_by(
                RoleMappingRule.provider.nulls_first(),
                RoleMappingRule.group,
                RoleMappingRule.id,
            )
        )
    )


def highest(roles: Iterable[UserRole]) -> UserRole | None:
    return max(roles, key=ROLE_RANK.__getitem__, default=None)


def mapped_role(
    rules: Iterable[Rule],
    default_role: UserRole,
    provider: str,
    groups: Iterable[str],
    provider_role: UserRole | None = None,
) -> UserRole:
    """Role for an identity of ``provider`` with ``groups`` (mapping on)."""
    folded = frozenset(g.casefold() for g in groups)
    matched = [rule.role for rule in rules if rule.matches(provider, folded)]
    # A role the provider derives itself (LDAP admin_groups) counts like a matching rule;
    # "user" from a provider is only its default and does not override the default role.
    if provider_role is not None and provider_role is not UserRole.USER:
        matched.append(provider_role)
    return highest(matched) or default_role


async def resolve_role(
    db: AsyncSession, provider: str, groups: Iterable[str], provider_role: UserRole | None
) -> UserRole | None:
    """The role to apply at this login; ``None`` leaves the user's role unchanged."""
    policy = await get_policy(db)
    if not policy.role_mapping_enabled:
        return provider_role
    rules = [Rule(r.group, r.provider, r.role) for r in await list_rules(db)]
    return mapped_role(rules, policy.default_role, provider, groups, provider_role)

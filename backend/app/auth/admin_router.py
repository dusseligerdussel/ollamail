"""Sign-in administration (admin only, #33): local login switch and group → role mapping.

Provider configuration itself lives with the providers (``/admin/auth/oidc``,
``/auth/ldap/directories``); every change there that can remove admin access is checked
by ``app.auth.admin_access``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import policy as policy_service
from app.auth.admin_access import AdminAccessGuard, admin_access
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.models import RoleMappingRule
from app.auth.providers import AuthProviderRegistry
from app.auth.schemas import (
    AdminAccess,
    AuthSettingsRead,
    AuthSettingsUpdate,
    RoleMappingRead,
    RoleMappingRuleRead,
    RoleMappingTest,
    RoleMappingTestResult,
    RoleMappingUpdate,
)
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import UserRole

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(
    prefix="/admin/auth",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

_LOCKOUT: dict[int | str, dict[str, Any]] = {
    409: {"description": "No administrator could sign in afterwards (admin-lockout)"}
}


def registry_of(request: Request) -> AuthProviderRegistry:
    registry: AuthProviderRegistry = request.app.state.auth_providers
    return registry


RegistryDep = Annotated[AuthProviderRegistry, Depends(registry_of)]


async def _settings_read(
    db: AsyncSession,
    registry: AuthProviderRegistry,
    request: Request,
    settings: SettingsDep,
    admin: AdminSessionDep,
) -> AuthSettingsRead:
    policy = await policy_service.get_policy(db)
    access = await admin_access(db, registry)
    kinds: set[str] = getattr(request.app.state, "idp_kinds", set())
    return AuthSettingsRead(
        local_login_enabled=policy.local_login_enabled,
        local_registration=settings.auth.local_registration and policy.local_login_enabled,
        provider_kinds=sorted(kinds),
        admin_access=AdminAccess(
            usable_admins=len(access), own_providers=sorted(access.get(admin.user_id, set()))
        ),
        mfa_enforcement=policy.mfa_enforcement,
    )


@router.get("/settings")
async def get_auth_settings(
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> AuthSettingsRead:
    """Sign-in settings and which admins can currently sign in (counts only)."""
    return await _settings_read(db, registry, request, settings, admin)


@router.patch("/settings", responses=_LOCKOUT)
async def update_auth_settings(
    body: AuthSettingsUpdate,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    registry: RegistryDep,
    settings: SettingsDep,
) -> AuthSettingsRead:
    """Switch local login on or off and set which local accounts need a second factor.
    Switching local login off is refused (409) unless another admin access (external
    provider) keeps working."""
    if body.local_login_enabled is not None:
        guard = await AdminAccessGuard.start(db, registry)
        policy = await policy_service.get_policy_for_update(db)
        if policy.local_login_enabled != body.local_login_enabled:
            policy.local_login_enabled = body.local_login_enabled
            await guard.check()
            await audit.record(
                db,
                audit.Actor.user(admin.user_id),
                audit.AuditAction.IDP_CONFIG_CHANGED,
                audit.Target.of(audit.TargetType.SETTINGS, "auth"),
                {"kind": "local", "change": "enabled" if body.local_login_enabled else "disabled"},
            )
            await db.commit()
            log.info(
                "local_login_changed",
                enabled=body.local_login_enabled,
                by_user_id=admin.user_id,
            )
    if body.mfa_enforcement is not None:
        policy = await policy_service.get_policy_for_update(db)
        if policy.mfa_enforcement != body.mfa_enforcement:
            policy.mfa_enforcement = body.mfa_enforcement
            await audit.record(
                db,
                audit.Actor.user(admin.user_id),
                audit.AuditAction.IDP_CONFIG_CHANGED,
                audit.Target.of(audit.TargetType.SETTINGS, "auth"),
                {"kind": "mfa", "change": str(body.mfa_enforcement)},
            )
            await db.commit()
            log.info("mfa_enforcement_changed", enforcement=str(body.mfa_enforcement))
    return await _settings_read(db, registry, request, settings, admin)


async def _mapping_read(db: AsyncSession) -> RoleMappingRead:
    policy = await policy_service.get_policy(db)
    return RoleMappingRead(
        enabled=policy.role_mapping_enabled,
        default_role=policy.default_role,
        rules=[
            RoleMappingRuleRead(id=r.id, group=r.group, provider=r.provider, role=r.role)
            for r in await policy_service.list_rules(db)
        ],
    )


@router.get("/role-mapping")
async def get_role_mapping(_: AdminSessionDep, db: DbDep) -> RoleMappingRead:
    """Group → role rules, applied at every login with an external provider."""
    return await _mapping_read(db)


@router.put("/role-mapping", responses={422: {"description": "Duplicate rules"}})
async def update_role_mapping(
    body: RoleMappingUpdate, admin: AdminSessionDep, db: DbDep
) -> RoleMappingRead:
    """Replace the mapping. Takes effect at each user's next login; the last active admin
    is never demoted by it."""
    keys = [(rule.provider, rule.group.casefold()) for rule in body.rules]
    if len(set(keys)) != len(keys):
        raise ProblemError(
            422,
            detail="Each group can only have one rule per provider.",
            type="urn:ollamail:problem:duplicate-rule",
        )
    policy = await policy_service.get_policy_for_update(db)
    policy.role_mapping_enabled = body.enabled
    policy.default_role = body.default_role
    await db.execute(delete(RoleMappingRule))
    db.add_all(
        RoleMappingRule(group=rule.group, provider=rule.provider, role=rule.role)
        for rule in body.rules
    )
    await db.flush()
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.SETTINGS, "role_mapping"),
        {
            "kind": "role_mapping",
            "change": "updated",
            "enabled": body.enabled,
            "default_role": str(body.default_role),
            "rules": len(body.rules),
            "admin_rules": sum(rule.role is UserRole.ADMIN for rule in body.rules),
        },
    )
    await db.commit()
    log.info("role_mapping_updated", rules=len(body.rules), by_user_id=admin.user_id)
    return await _mapping_read(db)


@router.post("/role-mapping/test")
async def test_role_mapping(
    body: RoleMappingTest, _: AdminSessionDep, db: DbDep
) -> RoleMappingTestResult:
    """Which role a login with these groups would get under the saved mapping."""
    policy = await policy_service.get_policy(db)
    rules = [
        policy_service.Rule(r.group, r.provider, r.role)
        for r in await policy_service.list_rules(db)
    ]
    folded = frozenset(group.casefold() for group in body.groups)
    matched = [rule.group for rule in rules if rule.matches(body.provider, folded)]
    role = (
        policy_service.mapped_role(rules, policy.default_role, body.provider, body.groups)
        if policy.role_mapping_enabled
        else None
    )
    return RoleMappingTestResult(role=role, matched_groups=matched)

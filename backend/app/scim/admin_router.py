"""SCIM administration (admin only, #95): switch, linking providers, tokens.

Tokens are shown once at creation; the API only ever returns their hint. Changes are
recorded as ``idp.config_changed`` (kind ``scim``) in the audit log.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.dependencies import AdminSessionDep
from app.auth.reauth import ADMIN_REAUTH_RESPONSES, RecentAdminDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.scim import resources, tokens
from app.scim.models import ScimGroup, ScimToken, ScimUser
from app.scim.schemas import (
    ScimSettingsRead,
    ScimSettingsUpdate,
    ScimStats,
    ScimTokenCreate,
    ScimTokenIssued,
    ScimTokenRead,
)
from app.users.models import User

log = get_logger(__name__)

DbDep = Annotated[AsyncSession, Depends(get_db)]

MAX_TOKENS = 20

router = APIRouter(
    prefix="/admin/scim",
    tags=["admin"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)


def _token_read(token: ScimToken) -> ScimTokenRead:
    return ScimTokenRead(
        id=token.id,
        name=token.name,
        hint=token.hint,
        created_at=token.created_at,
        expires_at=token.expires_at,
        last_used_at=token.last_used_at,
    )


async def _settings_read(db: AsyncSession, request: Request) -> ScimSettingsRead:
    config = await tokens.get_config(db)
    rows = await db.scalars(select(ScimToken).order_by(ScimToken.created_at, ScimToken.id))
    users, active = (
        await db.execute(
            select(func.count(), func.count().filter(User.is_active))
            .select_from(ScimUser)
            .join(User, User.id == ScimUser.user_id)
        )
    ).one()
    groups = await db.scalar(select(func.count()).select_from(ScimGroup)) or 0
    return ScimSettingsRead(
        enabled=config.enabled,
        endpoint_url=resources.base_url(request),
        link_providers=list(config.link_providers),
        tokens=[_token_read(token) for token in rows],
        stats=ScimStats(users=users, active_users=active, groups=groups),
    )


async def _record(db: AsyncSession, admin: AdminSessionDep, details: dict[str, Any]) -> None:
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.SETTINGS, "scim"),
        {"kind": "scim", **details},
    )


@router.get("")
async def get_scim_settings(_: AdminSessionDep, request: Request, db: DbDep) -> ScimSettingsRead:
    """SCIM switch, endpoint URL, tokens (hints only) and counts of provisioned objects."""
    return await _settings_read(db, request)


@router.patch("", responses=ADMIN_REAUTH_RESPONSES)
async def update_scim_settings(
    body: ScimSettingsUpdate, admin: RecentAdminDep, request: Request, db: DbDep
) -> ScimSettingsRead:
    """Switch SCIM on or off; set the providers that may link logins to SCIM users. Needs a
    recent confirmation: a linking provider signs in to SCIM accounts by e-mail address."""
    config = await tokens.get_config_for_update(db)
    if body.enabled is not None and body.enabled != config.enabled:
        config.enabled = body.enabled
        await _record(db, admin, {"change": "enabled" if body.enabled else "disabled"})
    if body.link_providers is not None:
        providers = sorted(set(body.link_providers))
        if providers != sorted(config.link_providers):
            config.link_providers = providers
            await _record(db, admin, {"change": "link_providers", "providers": len(providers)})
    await db.commit()
    log.info("scim_settings_updated", enabled=config.enabled, by_user_id=admin.user_id)
    return await _settings_read(db, request)


@router.post(
    "/tokens",
    status_code=status.HTTP_201_CREATED,
    responses={**ADMIN_REAUTH_RESPONSES, 409: {"description": "Too many tokens"}},
)
async def create_scim_token(
    body: ScimTokenCreate, admin: RecentAdminDep, db: DbDep
) -> ScimTokenIssued:
    """Create a bearer token for an IdP. The secret is only in this response."""
    count = await db.scalar(select(func.count()).select_from(ScimToken)) or 0
    if count >= MAX_TOKENS:
        raise ProblemError(
            409,
            detail="Revoke a token before creating another one.",
            type="urn:ollamail:problem:too-many-tokens",
        )
    expires_at = (
        datetime.now(UTC) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    )
    issued = tokens.new_token(body.name, expires_at)
    db.add(issued.token)
    await db.flush()
    await _record(db, admin, {"change": "token_created", "token_id": str(issued.token.id)})
    await db.commit()
    await db.refresh(issued.token)
    log.info("scim_token_created", token_id=issued.token.id, by_user_id=admin.user_id)
    return ScimTokenIssued(token=_token_read(issued.token), secret=issued.secret)


@router.delete(
    "/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "No such token"}},
)
async def revoke_scim_token(token_id: uuid.UUID, admin: AdminSessionDep, db: DbDep) -> Response:
    """Revoke a token; requests with it fail from now on."""
    result = await db.execute(
        delete(ScimToken).where(ScimToken.id == token_id).returning(ScimToken.id)
    )
    if result.first() is None:
        raise ProblemError(404, detail="Token not found.")
    await _record(db, admin, {"change": "token_revoked", "token_id": str(token_id)})
    await db.commit()
    log.info("scim_token_revoked", token_id=token_id, by_user_id=admin.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

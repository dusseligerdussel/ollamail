"""User administration (admin only, #33): list, role, deactivation, sessions, invitations.

Mail contents are never exposed here (docs/PRIVACY.md: Admin ≠ Leser). Every change that
could leave the instance without a working admin login is refused (409 ``admin-lockout``,
``app.auth.admin_access``), including demoting or deactivating oneself as the last admin.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import invitations
from app.auth.admin_access import AdminAccessGuard
from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.models import LOCAL_PROVIDER, AuthSession, Identity
from app.auth.passwords import hash_password
from app.auth.policy import local_login_enabled
from app.auth.providers import AuthProviderRegistry
from app.auth.sessions import revoke_user_sessions
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User
from app.users.schemas import (
    AdminUserRead,
    AdminUserUpdate,
    InvitationIssued,
    UserCreate,
    UserInvite,
    UserRead,
)
from app.users.service import add_local_user, check_password_policy

log = get_logger(__name__)

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such user"}}


def _registry(request: Request) -> AuthProviderRegistry:
    registry: AuthProviderRegistry = request.app.state.auth_providers
    return registry


async def _admin_reads(db: AsyncSession, users: list[User]) -> list[AdminUserRead]:
    ids = [user.id for user in users]
    providers: dict[uuid.UUID, set[str]] = {}
    for user_id, provider, password_hash in await db.execute(
        select(Identity.user_id, Identity.provider, Identity.password_hash).where(
            Identity.user_id.in_(ids)
        )
    ):
        # A local identity without password is an invitation, not a way to sign in.
        if provider != LOCAL_PROVIDER or password_hash is not None:
            providers.setdefault(user_id, set()).add(provider)
    counts = await db.execute(
        select(AuthSession.user_id, func.count())
        .where(AuthSession.user_id.in_(ids), AuthSession.expires_at > func.now())
        .group_by(AuthSession.user_id)
    )
    sessions = {user_id: count for user_id, count in counts}
    pending = await invitations.pending_user_ids(db)
    return [
        AdminUserRead(
            **UserRead.model_validate(user).model_dump(),
            providers=sorted(providers.get(user.id, set())),
            invitation_pending=user.id in pending,
            active_sessions=sessions.get(user.id, 0),
        )
        for user in users
    ]


async def _admin_read(db: AsyncSession, user: User) -> AdminUserRead:
    (read,) = await _admin_reads(db, [user])
    return read


async def _user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    # A user being deleted (#177) is gone for admins already.
    if user is None or user.deletion_requested_at is not None:
        raise ProblemError(404, detail="User not found.")
    return user


@router.get("")
async def list_users(_: AdminSessionDep, db: DbDep) -> list[AdminUserRead]:
    """All users, ordered by e-mail address, with their sign-in methods."""
    users = list(
        await db.scalars(
            select(User).where(User.deletion_requested_at.is_(None)).order_by(User.email)
        )
    )
    return await _admin_reads(db, users)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "E-mail address taken"}},
)
async def create_user(
    body: UserCreate, admin: AdminSessionDep, db: DbDep, settings: SettingsDep
) -> UserRead:
    """Create a local account with an initial password."""
    check_password_policy(settings.auth, body.password)
    user = await add_local_user(
        db,
        email=body.email,
        display_name=body.display_name,
        password_hash=await hash_password(body.password),
        role=body.role,
        language=body.language,
        timezone=body.timezone,
    )
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.USER_CREATED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"role": user.role, "via": "admin"},
    )
    await db.commit()
    log.info("user_created", user_id=user.id, role=user.role, by_user_id=admin.user_id)
    return UserRead.model_validate(user)


def _local_login_off() -> ProblemError:
    return ProblemError(
        409,
        detail="Local login is disabled; invited users could not sign in.",
        type="urn:ollamail:problem:local-login-disabled",
    )


@router.post(
    "/invitations",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "E-mail address taken or local login disabled"}},
)
async def invite_user(
    body: UserInvite, admin: AdminSessionDep, request: Request, db: DbDep, settings: SettingsDep
) -> InvitationIssued:
    """Create a local account without password and return a one-time invitation link."""
    if not await local_login_enabled(db):
        raise _local_login_off()
    user = await add_local_user(
        db,
        email=body.email,
        display_name=body.display_name,
        password_hash=None,
        role=body.role,
        language=body.language,
        timezone=body.timezone,
    )
    issued = await invitations.issue(db, settings, request, user)
    actor = audit.Actor.user(admin.user_id)
    target = audit.Target.of(audit.TargetType.USER, user.id)
    await audit.record(
        db, actor, audit.AuditAction.USER_CREATED, target, {"role": user.role, "via": "invitation"}
    )
    await audit.record(db, actor, audit.AuditAction.USER_INVITED, target)
    await db.commit()
    log.info("user_invited", user_id=user.id, role=user.role, by_user_id=admin.user_id)
    return InvitationIssued(
        user=await _admin_read(db, user), invite_url=issued.url, expires_at=issued.expires_at
    )


@router.post(
    "/{user_id}/invitation",
    responses={**_NOT_FOUND, 409: {"description": "The user already has a password"}},
)
async def reissue_invitation(
    user_id: uuid.UUID,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> InvitationIssued:
    """New invitation link for an invited user who has not set a password yet (the old
    link stops working)."""
    if not await local_login_enabled(db):
        raise _local_login_off()
    user = await _user(db, user_id)
    identity = await db.scalar(
        select(Identity).where(Identity.user_id == user.id, Identity.provider == LOCAL_PROVIDER)
    )
    if identity is None or identity.password_hash is not None:
        raise ProblemError(
            409,
            detail="Only invited users without a password can get a new invitation.",
            type="urn:ollamail:problem:not-invited",
        )
    issued = await invitations.issue(db, settings, request, user)
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.USER_INVITED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"renewed": True},
    )
    await db.commit()
    log.info("user_invitation_renewed", user_id=user.id, by_user_id=admin.user_id)
    return InvitationIssued(
        user=await _admin_read(db, user), invite_url=issued.url, expires_at=issued.expires_at
    )


@router.patch(
    "/{user_id}",
    responses={
        **_NOT_FOUND,
        409: {"description": "No administrator could sign in afterwards (admin-lockout)"},
    },
)
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    admin: AdminSessionDep,
    request: Request,
    db: DbDep,
) -> AdminUserRead:
    """Change role or active state. Deactivating ends all sessions of the user."""
    guard = await AdminAccessGuard.start(db, _registry(request))
    user = await _user(db, user_id)
    actor = audit.Actor.user(admin.user_id)
    target = audit.Target.of(audit.TargetType.USER, user.id)
    events: list[tuple[audit.AuditAction, dict[str, Any] | None]] = []
    if body.role is not None and body.role != user.role:
        events.append(
            (
                audit.AuditAction.USER_ROLE_CHANGED,
                {"from_role": str(user.role), "to_role": str(body.role), "via": "admin"},
            )
        )
        user.role = body.role
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        if body.is_active:
            events.append((audit.AuditAction.USER_REACTIVATED, None))
        else:
            count = await revoke_user_sessions(db, user.id)
            events.append((audit.AuditAction.USER_DEACTIVATED, {"sessions": count}))
    if events:
        await guard.check()
        for action, details in events:
            await audit.record(db, actor, action, target, details)
        await db.commit()
        await db.refresh(user)
        log.info(
            "user_updated",
            user_id=user.id,
            changes=[str(action) for action, _ in events],
            by_user_id=admin.user_id,
        )
    return await _admin_read(db, user)


@router.delete("/{user_id}/sessions", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
async def revoke_sessions(user_id: uuid.UUID, admin: AdminSessionDep, db: DbDep) -> Response:
    """Sign the user out everywhere (also the admin's own sessions, if it is them)."""
    user = await _user(db, user_id)
    count = await revoke_user_sessions(db, user.id)
    await audit.record(
        db,
        audit.Actor.user(admin.user_id),
        audit.AuditAction.SESSION_REVOKED,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"count": count, "via": "admin"},
    )
    await db.commit()
    log.info("user_sessions_revoked", user_id=user.id, count=count, by_user_id=admin.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

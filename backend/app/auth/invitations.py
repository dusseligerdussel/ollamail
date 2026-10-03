"""Invitations for local accounts (#33).

An admin invites someone by e-mail address: the account is created with a local identity
but no password, and the admin gets a one-time link to pass on (ollamail sends no mail
itself). The link carries 256 random bits in the URL fragment (``/invite#<token>``), so it
never reaches server logs or ``Referer`` headers; only its SHA-256 is stored. Accepting
sets the password, deletes the invitation and continues like a login: with a second
factor (or while 2FA is enforced for the account) the next step is ``/auth/mfa`` (202
``MfaChallenge``), otherwise the user is signed in. A new link replaces the old one.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth import service
from app.auth.dependencies import SettingsDep
from app.auth.mfa import service as mfa_service
from app.auth.mfa.router import second_step as mfa_second_step
from app.auth.mfa.schemas import MfaChallenge
from app.auth.models import LOCAL_PROVIDER, Identity, Invitation
from app.auth.passwords import hash_password
from app.auth.policy import local_login_enabled
from app.auth.redirect_flow import public_origin
from app.auth.sessions import hash_token
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.users.models import User
from app.users.schemas import Password, UserRead
from app.users.service import check_password_policy

log = get_logger(__name__)

INVITE_PAGE = "/invite"
_TOKEN = Annotated[str, Field(min_length=16, max_length=128)]


@dataclass(frozen=True)
class IssuedInvitation:
    url: str
    expires_at: datetime


async def issue(
    db: AsyncSession, settings: Settings, request: Request, user: User
) -> IssuedInvitation:
    """Create (or replace) the invitation of ``user`` (caller commits)."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(hours=settings.auth.invitation_lifetime_hours)
    await db.execute(delete(Invitation).where(Invitation.user_id == user.id))
    db.add(Invitation(user_id=user.id, token_hash=hash_token(token), expires_at=expires_at))
    await db.flush()
    url = f"{public_origin(settings, request)}{INVITE_PAGE}#{token}"
    return IssuedInvitation(url=url, expires_at=expires_at)


async def pending_user_ids(db: AsyncSession) -> set[uuid.UUID]:
    """Users with an invitation that has not expired."""
    rows = await db.scalars(
        select(Invitation.user_id).where(Invitation.expires_at > datetime.now(UTC))
    )
    return set(rows)


async def _find(db: AsyncSession, token: str) -> tuple[Invitation, User] | None:
    row = (
        await db.execute(
            select(Invitation, User)
            .join(User, User.id == Invitation.user_id)
            .where(
                Invitation.token_hash == hash_token(token),
                Invitation.expires_at > datetime.now(UTC),
                User.is_active,
            )
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


def _invalid() -> ProblemError:
    return ProblemError(
        404,
        detail="The invitation is invalid or has expired.",
        type="urn:ollamail:problem:invitation-invalid",
    )


# -- Public endpoints -------------------------------------------------------------------

DbDep = Annotated[AsyncSession, Depends(get_db)]

router = APIRouter(prefix="/auth/invitations", tags=["auth"])


class InvitationLookup(BaseModel):
    token: _TOKEN


class InvitationInfo(BaseModel):
    email: str
    display_name: str
    expires_at: datetime


class InvitationAccept(BaseModel):
    token: _TOKEN
    password: Password


_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"description": "Local login is disabled"},
    404: {"description": "Invalid or expired invitation"},
    429: {"description": "Too many attempts"},
}


# POST instead of GET: the token must not end up in access logs as a query parameter.
@router.post("/lookup", responses=_ERRORS)
async def lookup_invitation(
    body: InvitationLookup, request: Request, db: DbDep, settings: SettingsDep
) -> InvitationInfo:
    """Who the invitation is for (shown on the "set password" page)."""
    await service.throttle_ip(db, settings, request)
    found = await _find(db, body.token)
    if found is None:
        raise _invalid()
    invitation, user = found
    return InvitationInfo(
        email=user.email, display_name=user.display_name, expires_at=invitation.expires_at
    )


@router.post(
    "/accept",
    status_code=status.HTTP_200_OK,
    response_model=UserRead,
    responses={
        **_ERRORS,
        202: {"model": MfaChallenge, "description": "Password set, second step needed"},
    },
)
async def accept_invitation(
    body: InvitationAccept,
    request: Request,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> UserRead | JSONResponse:
    """Set the password of the invited account and sign in. Like ``POST /auth/login``,
    accounts with a second factor or under enforced 2FA get 202 and no session yet."""
    await service.throttle_ip(db, settings, request)
    if not await local_login_enabled(db):
        raise ProblemError(
            403,
            detail="Sign-in with local accounts is disabled.",
            type="urn:ollamail:problem:local-login-disabled",
        )
    found = await _find(db, body.token)
    if found is None:
        log.info("invitation_rejected", reason="invalid")
        raise _invalid()
    invitation, user = found
    check_password_policy(settings.auth, body.password)
    identity = await db.scalar(
        select(Identity).where(Identity.user_id == user.id, Identity.provider == LOCAL_PROVIDER)
    )
    password_hash = await hash_password(body.password)
    if identity is None:
        db.add(
            Identity(
                user_id=user.id,
                provider=LOCAL_PROVIDER,
                subject=str(user.id),
                password_hash=password_hash,
            )
        )
    else:
        identity.password_hash = password_hash
    await db.delete(invitation)
    await audit.record(
        db,
        audit.Actor.user(user.id),
        audit.AuditAction.USER_PASSWORD_SET,
        audit.Target.of(audit.TargetType.USER, user.id),
        {"via": "invitation"},
    )
    step = await mfa_service.login_step(db, user)
    if step is not None:
        # Commits the password together with the pending second step.
        log.info("invitation_accepted", user_id=user.id, second_step=step)
        return await mfa_second_step(db, settings, request, user, step)
    await service.start_session(db, settings, request, response, user, provider=LOCAL_PROVIDER)
    log.info("invitation_accepted", user_id=user.id)
    return UserRead.model_validate(user)

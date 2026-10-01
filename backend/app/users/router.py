"""User administration (admin only). Role changes, deactivation and invitations follow in
#33; mail contents are never exposed here (docs/PRIVACY.md: Admin ≠ Leser)."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import AdminSessionDep, SettingsDep
from app.auth.passwords import hash_password
from app.core.db import get_db
from app.core.logging import get_logger
from app.users.models import User
from app.users.schemas import UserCreate, UserRead
from app.users.service import add_local_user, check_password_policy

log = get_logger(__name__)

router = APIRouter(
    prefix="/users",
    tags=["users"],
    responses={401: {"description": "Not signed in"}, 403: {"description": "Not an admin"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("")
async def list_users(_: AdminSessionDep, db: DbDep) -> list[UserRead]:
    """All users, ordered by e-mail address."""
    users = await db.scalars(select(User).order_by(User.email))
    return [UserRead.model_validate(user) for user in users]


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
    await db.commit()
    log.info("user_created", user_id=user.id, role=user.role, by_user_id=admin.user_id)
    return UserRead.model_validate(user)

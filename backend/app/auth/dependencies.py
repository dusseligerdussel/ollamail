"""FastAPI dependencies for authentication and authorisation.

``get_current_session`` resolves the session cookie; ``require_admin`` additionally checks
the role. ``app.core.current_user.get_current_user_id`` builds on them. The database
session used here closes as soon as the session is resolved (``scope="function"``), so
long-running responses such as the SSE stream do not hold a pooled connection.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import SESSION_COOKIE, CurrentSession, resolve_session
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.users.models import User


def get_settings_from_app(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_current_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
) -> CurrentSession:
    """The authenticated session; 401 without a valid session cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    current = None
    if token:
        current = await resolve_session(db, get_settings_from_app(request).auth, token)
    if current is None:
        raise ProblemError(401, detail="Authentication required.")
    return current


async def require_admin(
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> CurrentSession:
    """The authenticated session of an admin; 403 for other users."""
    if not current.is_admin:
        raise ProblemError(403, detail="Administrator role required.")
    return current


async def get_current_user(
    current: Annotated[CurrentSession, Depends(get_current_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """The authenticated user as ORM object (for endpoints that read or change it)."""
    user = await db.get(User, current.user_id)
    if user is None:
        raise ProblemError(401, detail="Authentication required.")
    return user


CurrentSessionDep = Annotated[CurrentSession, Depends(get_current_session)]
AdminSessionDep = Annotated[CurrentSession, Depends(require_admin)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]

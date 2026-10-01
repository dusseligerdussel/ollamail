"""The authenticated user of a request.

``get_current_user_id`` resolves the session cookie (``app.auth``) and rejects requests
without a valid session with 401. Tests override it via ``app.dependency_overrides``.
Endpoints must take the user only from this dependency (or ``app.auth.dependencies``),
never from request parameters.
"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends

from app.auth.dependencies import get_current_session
from app.auth.sessions import CurrentSession


async def get_current_user_id(
    current: Annotated[CurrentSession, Depends(get_current_session)],
) -> UUID:
    """ID of the authenticated user; 401 if the request is not authenticated."""
    return current.user_id

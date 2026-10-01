"""The authenticated user of a request.

Placeholder until authentication exists (#11): ``get_current_user_id`` rejects every
request with 401, so endpoints that depend on it are closed by default. #11 replaces the
body with the session lookup; tests override it via ``app.dependency_overrides``.
Endpoints must take the user only from this dependency, never from request parameters.
"""

from uuid import UUID

from app.core.errors import ProblemError


async def get_current_user_id() -> UUID:
    """ID of the authenticated user; 401 if the request is not authenticated."""
    raise ProblemError(401, detail="Authentication required.")

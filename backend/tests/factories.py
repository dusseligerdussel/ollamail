"""Test data factories shared across modules."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.users.models import User, UserRole


async def make_user(
    session: AsyncSession, *, role: UserRole = UserRole.USER, **kwargs: object
) -> User:
    """A user without identities (cannot sign in); for ownership in other modules' tests."""
    values: dict[str, object] = {
        "email": f"user-{uuid.uuid4().hex[:12]}@example.org",
        "display_name": "Test User",
        "role": role,
    }
    values.update(kwargs)
    user = User(**values)
    session.add(user)
    await session.flush()
    return user

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers import VerifiedIdentity
from app.auth.provisioning import provision_user
from app.core.errors import ProblemError
from app.users.models import UserRole
from tests.auth.conftest import make_local_user

pytestmark = pytest.mark.db


def _identity(**changes: str) -> VerifiedIdentity:
    values = {"provider": "ldap:test", "subject": "guid-1", "email": " New.User@Example.org "}
    values.update(changes)
    return VerifiedIdentity(**values)  # type: ignore[arg-type]


async def test_creates_user_once(db_session: AsyncSession) -> None:
    first = await provision_user(db_session, _identity())
    second = await provision_user(db_session, _identity(email="changed@example.org"))

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.email == "new.user@example.org"
    # Without a display name the local part of the address is used.
    assert first.display_name == "new.user"
    assert first.role is UserRole.USER


async def test_role_is_only_changed_when_given(db_session: AsyncSession) -> None:
    user = await provision_user(db_session, _identity(), role=UserRole.ADMIN)
    assert user is not None and user.role is UserRole.ADMIN

    unchanged = await provision_user(db_session, _identity())
    assert unchanged is not None and unchanged.role is UserRole.ADMIN

    demoted = await provision_user(db_session, _identity(), role=UserRole.USER)
    assert demoted is not None and demoted.role is UserRole.USER


async def test_does_not_link_existing_accounts(db_session: AsyncSession) -> None:
    await make_local_user(db_session, "new.user@example.org", role=UserRole.ADMIN)

    with pytest.raises(ProblemError) as raised:
        await provision_user(db_session, _identity())
    assert raised.value.status == 409


async def test_requires_email(db_session: AsyncSession) -> None:
    with pytest.raises(ProblemError) as raised:
        await provision_user(db_session, _identity(email=""))
    assert raised.value.status == 403


async def test_inactive_user(db_session: AsyncSession) -> None:
    user = await provision_user(db_session, _identity())
    assert user is not None
    user.is_active = False

    assert await provision_user(db_session, _identity()) is None

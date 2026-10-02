"""User administration (#33): roles, deactivation, sessions and the last-admin protection."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import AuthSession, Identity
from app.auth.sessions import create_session
from app.core.config import Settings
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user

pytestmark = pytest.mark.db

LOCKOUT = "urn:ollamail:problem:admin-lockout"


async def _admin(client: AsyncClient, db: AsyncSession) -> User:
    admin = await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(client, admin.email)).status_code == 200
    return admin


async def _sign_in_elsewhere(db: AsyncSession, settings: Settings, user: User) -> None:
    await create_session(db, settings.auth, user, provider="local", user_agent=None)
    await db.commit()


async def _sessions(db: AsyncSession, user: User) -> int:
    query = select(func.count()).select_from(AuthSession).where(AuthSession.user_id == user.id)
    return int(await db.scalar(query) or 0)


async def test_requires_admin(db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_local_user(db_session)
    await login(db_client, user.email)

    assert (await db_client.patch(f"/users/{user.id}", json={"role": "admin"})).status_code == 403
    assert (await db_client.delete(f"/users/{user.id}/sessions")).status_code == 403
    assert (await db_client.post("/users/invitations", json={})).status_code in {403, 422}


async def test_list_shows_sign_in_methods_without_mail_data(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _admin(db_client, db_session)
    erika = await make_local_user(db_session)
    db_session.add(Identity(user_id=erika.id, provider="oidc:corp", subject="s-1"))
    await db_session.commit()

    users = (await db_client.get("/users")).json()

    by_email = {user["email"]: user for user in users}
    assert by_email["erika@example.org"]["providers"] == ["local", "oidc:corp"]
    assert by_email["admin@example.org"]["active_sessions"] == 1
    assert by_email["admin@example.org"]["id"] == str(admin.id)
    assert set(by_email["erika@example.org"]) == {
        "id",
        "email",
        "display_name",
        "role",
        "language",
        "timezone",
        "is_active",
        "created_at",
        "last_login_at",
        "providers",
        "invitation_pending",
        "active_sessions",
    }


async def test_change_role_is_audited(db_client: AsyncClient, db_session: AsyncSession) -> None:
    admin = await _admin(db_client, db_session)
    erika = await make_local_user(db_session)

    response = await db_client.patch(f"/users/{erika.id}", json={"role": "admin"})

    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    (row,) = await audit_rows(db_session, AuditAction.USER_ROLE_CHANGED)
    assert row.actor_id == admin.id
    assert row.target_id == str(erika.id)
    assert row.details == {"from_role": "user", "to_role": "admin", "via": "admin"}


async def test_deactivate_ends_sessions_and_blocks_sign_in(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _admin(db_client, db_session)
    erika = await make_local_user(db_session)
    await _sign_in_elsewhere(db_session, settings, erika)
    assert await _sessions(db_session, erika) == 1

    response = await db_client.patch(f"/users/{erika.id}", json={"is_active": False})

    assert response.status_code == 200
    assert response.json()["is_active"] is False
    assert await _sessions(db_session, erika) == 0
    assert (await login(db_client, erika.email)).status_code == 401
    (row,) = await audit_rows(db_session, AuditAction.USER_DEACTIVATED)
    assert row.details == {"sessions": 1}
    reactivated = await db_client.patch(f"/users/{erika.id}", json={"is_active": True})
    assert reactivated.json()["is_active"] is True
    assert len(await audit_rows(db_session, AuditAction.USER_REACTIVATED)) == 1


async def test_last_admin_cannot_demote_or_deactivate_themselves(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin_id = (await _admin(db_client, db_session)).id

    demote = await db_client.patch(f"/users/{admin_id}", json={"role": "user"})
    deactivate = await db_client.patch(f"/users/{admin_id}", json={"is_active": False})

    assert (demote.status_code, demote.json()["type"]) == (409, LOCKOUT)
    assert (deactivate.status_code, deactivate.json()["type"]) == (409, LOCKOUT)
    row = (await db_session.execute(select(User.role, User.is_active))).one()
    assert tuple(row) == (UserRole.ADMIN, True)
    assert await audit_rows(db_session, AuditAction.USER_ROLE_CHANGED) == []
    assert await audit_rows(db_session, AuditAction.USER_DEACTIVATED) == []
    # The admin is still signed in.
    assert (await db_client.get("/users")).status_code == 200


async def test_admin_without_working_login_does_not_count(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A second admin who can only sign in through a provider that does not exist (any
    more) does not keep the instance accessible."""
    admin = await _admin(db_client, db_session)
    other = await make_local_user(db_session, "other@example.org", role=UserRole.ADMIN)
    await db_session.execute(Identity.__table__.delete().where(Identity.user_id == other.id))
    db_session.add(Identity(user_id=other.id, provider="oidc:gone", subject="x"))
    await db_session.commit()

    response = await db_client.patch(f"/users/{admin.id}", json={"role": "user"})

    assert response.status_code == 409


async def test_admin_can_step_down_when_another_admin_remains(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _admin(db_client, db_session)
    await make_local_user(db_session, "other@example.org", role=UserRole.ADMIN)

    response = await db_client.patch(f"/users/{admin.id}", json={"role": "user"})

    assert response.status_code == 200
    # The role is checked on every request: the former admin lost access right away.
    assert (await db_client.get("/users")).status_code == 403


async def test_revoke_all_sessions_of_a_user(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    admin = await _admin(db_client, db_session)
    erika = await make_local_user(db_session)
    await _sign_in_elsewhere(db_session, settings, erika)

    response = await db_client.delete(f"/users/{erika.id}/sessions")

    assert response.status_code == 204
    assert await _sessions(db_session, erika) == 0
    assert await _sessions(db_session, admin) == 1
    (row,) = await audit_rows(db_session, AuditAction.SESSION_REVOKED)
    assert (row.actor_id, row.target_id) == (admin.id, str(erika.id))
    assert row.details == {"count": 1, "via": "admin"}


async def test_unknown_user(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _admin(db_client, db_session)
    missing = "01900000-0000-7000-8000-000000000000"

    assert (await db_client.patch(f"/users/{missing}", json={"role": "user"})).status_code == 404
    assert (await db_client.delete(f"/users/{missing}/sessions")).status_code == 404

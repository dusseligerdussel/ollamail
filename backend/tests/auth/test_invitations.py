"""Invitations for local accounts (#33)."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import Invitation
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import PASSWORD, login, make_local_user

pytestmark = pytest.mark.db

INVITE = {"email": "Max@Example.org", "display_name": "Max Muster", "role": "user"}


async def _invite(client: AsyncClient, db: AsyncSession) -> tuple[str, str]:
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    await login(client, "admin@example.org")
    response = await client.post("/users/invitations", json=INVITE)
    assert response.status_code == 201, response.text
    body = response.json()
    url: str = body["invite_url"]
    assert url.startswith("https://test/invite#")
    return body["user"]["id"], url.partition("#")[2]


async def test_invited_user_sets_password_and_signs_in(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, token = await _invite(db_client, db_session)
    users = {u["email"]: u for u in (await db_client.get("/users")).json()}
    assert users["max@example.org"]["invitation_pending"] is True
    assert users["max@example.org"]["providers"] == []
    await db_client.post("/auth/logout")
    # Without a password the account cannot sign in.
    assert (await login(db_client, "max@example.org")).status_code == 401

    info = await db_client.post("/auth/invitations/lookup", json={"token": token})
    accepted = await db_client.post(
        "/auth/invitations/accept", json={"token": token, "password": PASSWORD}
    )

    assert info.json()["email"] == "max@example.org"
    assert accepted.status_code == 200
    assert accepted.json()["id"] == user_id
    assert (await db_client.get("/auth/me")).json()["email"] == "max@example.org"
    # One-time link.
    again = await db_client.post("/auth/invitations/lookup", json={"token": token})
    assert again.status_code == 404
    await db_client.post("/auth/logout")
    assert (await login(db_client, "max@example.org")).status_code == 200
    (row,) = await audit_rows(db_session, AuditAction.USER_PASSWORD_SET)
    assert row.details == {"via": "invitation"}
    assert len(await audit_rows(db_session, AuditAction.USER_INVITED)) == 1


async def test_token_hash_only_is_stored(db_client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _invite(db_client, db_session)

    stored = await db_session.scalar(select(Invitation.token_hash))

    assert stored is not None and token.encode() not in stored


async def test_expired_or_wrong_token_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, token = await _invite(db_client, db_session)
    await db_session.execute(
        update(Invitation).values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.commit()

    expired = await db_client.post(
        "/auth/invitations/accept", json={"token": token, "password": PASSWORD}
    )
    wrong = await db_client.post("/auth/invitations/lookup", json={"token": "x" * 43})

    assert expired.status_code == 404
    assert wrong.status_code == 404


async def test_new_link_replaces_the_old_one(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id, old = await _invite(db_client, db_session)

    renewed = await db_client.post(f"/users/{user_id}/invitation")

    new = renewed.json()["invite_url"].partition("#")[2]
    assert new != old
    lookup = await db_client.post("/auth/invitations/lookup", json={"token": old})
    assert lookup.status_code == 404
    lookup = await db_client.post("/auth/invitations/lookup", json={"token": new})
    assert lookup.status_code == 200


async def test_no_new_link_for_users_with_password(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _invite(db_client, db_session)
    erika = await make_local_user(db_session)

    response = await db_client.post(f"/users/{erika.id}/invitation")

    assert response.status_code == 409


async def test_short_password_is_rejected(db_client: AsyncClient, db_session: AsyncSession) -> None:
    _, token = await _invite(db_client, db_session)

    response = await db_client.post(
        "/auth/invitations/accept", json={"token": token, "password": "short-pw"}
    )

    assert response.status_code == 422
    lookup = await db_client.post("/auth/invitations/lookup", json={"token": token})
    assert lookup.status_code == 200

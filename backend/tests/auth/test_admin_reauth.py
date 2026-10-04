"""Critical admin actions need a recent confirmation (#190, #206, app/auth/reauth.py).

A stolen admin cookie alone must not be enough to add an identity provider that links to
other accounts, to point an AI endpoint at a foreign server, to take over accounts or to
read a shared mailbox.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.mail.models import Mailbox, MailboxAssignment, MailboxType
from app.users.models import User, UserRole
from tests.auth.conftest import PASSWORD, login, make_local_user

pytestmark = pytest.mark.db

REAUTH_REQUIRED = "urn:ollamail:problem:reauth-required"
VICTIM = "erika@example.org"

# (method, path, body); ``{victim}`` is the ID of a second user. The bodies are valid where
# it matters, so a missing check would show up as a success, not as a validation error.
CRITICAL = [
    ("DELETE", "/admin/privacy/users/{victim}", None),
    ("PATCH", "/users/{victim}", {"role": "admin"}),
    ("PATCH", "/users/{victim}", {"is_active": False}),
    ("POST", "/admin/scim/tokens", {"name": "Entra ID"}),
    ("PATCH", "/admin/auth/settings", {"local_login_enabled": True}),
    ("POST", "/admin/auth/oidc/providers", {}),
    ("PATCH", "/admin/auth/oidc/providers/corp", {}),
    ("POST", "/admin/auth/github/providers", {}),
    ("PATCH", "/admin/auth/github/providers/corp", {}),
    ("POST", "/admin/auth/saml/providers", {}),
    ("PATCH", "/admin/auth/saml/providers/corp", {}),
    ("POST", "/auth/ldap/directories", {}),
    ("PUT", "/auth/ldap/directories/corp", {}),
    (
        "POST",
        "/admin/ai/providers",
        {
            "name": "relay",
            "display_name": "Relay",
            "kind": "openai",
            "base_url": "https://llm.example.org/v1",
        },
    ),
    ("PATCH", "/admin/ai/providers/relay", {"base_url": "https://llm.example.org/v1"}),
]

# Settings that open a way to other accounts or to mail contents (#206), and how they read
# before the request; a missing check would change them.
SETTINGS = [
    (
        "PUT",
        "/admin/auth/role-mapping",
        {
            "enabled": True,
            "default_role": "user",
            "rules": [{"group": "staff", "provider": None, "role": "admin"}],
        },
        "/admin/auth/role-mapping",
        {"enabled": False, "rules": []},
    ),
    ("PATCH", "/admin/scim", {"link_providers": ["corp"]}, "/admin/scim", {"link_providers": []}),
    (
        "PATCH",
        "/admin/ai/settings",
        {"cloud_enabled": True},
        "/admin/ai/settings",
        {"cloud_enabled": False},
    ),
    (
        "PATCH",
        "/admin/ai/settings",
        {"tasks": {"triage": {"provider": None, "model": "elsewhere"}}},
        "/admin/ai/settings",
        {"cloud_enabled": False},
    ),
]


async def _age_sessions(db: AsyncSession, minutes: int = 11) -> None:
    """Pretend the sign-in was ``minutes`` ago (default limit: 10)."""
    await db.execute(
        update(AuthSession).values(authenticated_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )
    await db.commit()


async def _stale_admin(client: AsyncClient, db: AsyncSession) -> uuid.UUID:
    """A second user (its ID), and an admin whose sign-in is older than the limit."""
    victim_id = (await make_local_user(db, VICTIM)).id
    admin = await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(client, admin.email)).status_code == 200
    await _age_sessions(db)
    return victim_id


@pytest.mark.parametrize(("method", "path", "body"), CRITICAL)
async def test_critical_admin_actions_need_a_recent_confirmation(
    db_client: AsyncClient, db_session: AsyncSession, method: str, path: str, body: object
) -> None:
    victim_id = await _stale_admin(db_client, db_session)

    response = await db_client.request(method, path.format(victim=victim_id), json=body)

    assert response.status_code == 403, response.text
    assert response.json()["type"] == REAUTH_REQUIRED
    assert response.json()["reauth_minutes"] == 10
    # Nothing happened to the other user.
    db_session.expire_all()
    stored = await db_session.scalar(select(User).where(User.id == victim_id))
    assert stored is not None
    assert stored.role is UserRole.USER
    assert stored.is_active


@pytest.mark.parametrize(("method", "path", "body", "read_path", "before"), SETTINGS)
async def test_sensitive_settings_need_a_recent_confirmation(
    db_client: AsyncClient,
    db_session: AsyncSession,
    method: str,
    path: str,
    body: object,
    read_path: str,
    before: dict[str, object],
) -> None:
    await _stale_admin(db_client, db_session)

    response = await db_client.request(method, path, json=body)

    assert response.status_code == 403, response.text
    assert response.json()["type"] == REAUTH_REQUIRED
    stored = (await db_client.get(read_path)).json()
    assert {key: stored[key] for key in before} == before
    if path == "/admin/ai/settings":
        assert all(task["model"] is None for task in stored["tasks"])


async def _shared_mailbox(db: AsyncSession) -> uuid.UUID:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Support",
        address="support@example.org",
        owner_user_id=None,
        is_shared=True,
    )
    db.add(mailbox)
    await db.commit()
    return mailbox.id


async def test_assigning_a_shared_mailbox_needs_a_recent_confirmation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    victim_id = await _stale_admin(db_client, db_session)
    mailbox_id = await _shared_mailbox(db_session)
    admin_id = await db_session.scalar(select(User.id).where(User.email == "admin@example.org"))

    for users in ([str(admin_id)], [str(victim_id)]):
        response = await db_client.put(
            f"/admin/shared-mailboxes/{mailbox_id}/assignments", json={"users": users}
        )
        assert response.status_code == 403, response.text
        assert response.json()["type"] == REAUTH_REQUIRED
    # Adding one with first assignments neither.
    created = await db_client.post(
        "/admin/shared-mailboxes",
        json={"type": "imap", "address": "team@example.org", "users": [str(admin_id)]},
    )
    assert created.status_code == 403, created.text
    assert created.json()["type"] == REAUTH_REQUIRED

    assert await db_session.scalar(select(MailboxAssignment.id)) is None
    assert (
        await db_session.scalar(select(Mailbox.id).where(Mailbox.address == "team@example.org"))
        is None
    )


async def test_non_admins_get_the_admin_error_first(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = (await make_local_user(db_session, VICTIM)).id
    assert (await login(db_client, VICTIM)).status_code == 200
    await _age_sessions(db_session)

    response = await db_client.patch(f"/users/{user_id}", json={"role": "admin"})

    assert response.status_code == 403
    # Confirming would not help: no reauth-required, so the UI shows no confirmation sheet.
    assert response.json().get("type") != REAUTH_REQUIRED


async def test_reading_and_harmless_actions_need_no_confirmation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    victim_id = await _stale_admin(db_client, db_session)

    assert (await db_client.get("/users")).status_code == 200
    assert (await db_client.get("/admin/auth/settings")).status_code == 200
    assert (await db_client.get("/admin/ai/providers")).status_code == 200
    # Ending someone's sessions only locks out, it does not give access.
    assert (await db_client.delete(f"/users/{victim_id}/sessions")).status_code == 204
    # Turning cloud providers off and tuning performance send nothing elsewhere (#206).
    tuned = await db_client.patch(
        "/admin/ai/settings", json={"cloud_enabled": False, "concurrency": 1}
    )
    assert tuned.status_code == 200, tuned.text
    # A shared mailbox without assignments gives nobody access; the request gets as far
    # as the connection test.
    added = await db_client.post(
        "/admin/shared-mailboxes", json={"type": "imap", "address": "team@example.org"}
    )
    assert added.json().get("type") != REAUTH_REQUIRED, added.text


async def test_a_confirmation_unlocks_the_actions(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    victim_id = await _stale_admin(db_client, db_session)

    confirmed = await db_client.post(
        "/auth/reauth", json={"method": "password", "password": PASSWORD}
    )
    changed = await db_client.patch(f"/users/{victim_id}", json={"role": "admin"})

    assert confirmed.status_code == 200, confirmed.text
    assert changed.status_code == 200, changed.text
    assert changed.json()["role"] == "admin"

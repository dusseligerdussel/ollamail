"""Notification API: integration tests with real sessions and PostgreSQL (#149)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import NotificationsSettings, Settings
from app.triage.models import TriageCategory, TriageSource
from app.triage.service import Decision, save_result
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.notifications.conftest import builtin_category
from tests.triage.conftest import Account, account_for, make_account

pytestmark = pytest.mark.db


async def _signed_in(client: AsyncClient, session: AsyncSession) -> Account:
    user = await make_local_user(session, f"user-{uuid.uuid4().hex[:6]}@example.org")
    assert (await login(client, user.email)).status_code == 200
    return await account_for(session, user)


async def test_requires_authentication(db_client: AsyncClient) -> None:
    assert (await db_client.get("/notifications/settings")).status_code == 401
    assert (await db_client.put("/notifications/settings", json={})).status_code == 401
    assert (await db_client.get(f"/notifications/messages/{uuid.uuid4()}")).status_code == 401


async def test_settings_roundtrip(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)
    assert (await db_client.get("/notifications/settings")).json() == {
        "available": True,
        "enabled": False,
        "category_ids": [],
        "show_subject": False,
        "sound": False,
    }
    important = str(await builtin_category(db_session, "important"))
    updated = await db_client.put(
        "/notifications/settings",
        json={"enabled": True, "category_ids": [important, important]},
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "available": True,
        "enabled": True,
        "category_ids": [important],
        "show_subject": False,
        "sound": False,
    }
    # Fields left out stay as they are.
    updated = await db_client.put("/notifications/settings", json={"show_subject": True})
    assert (updated.json()["enabled"], updated.json()["show_subject"]) == (True, True)
    assert (await db_client.get("/notifications/settings")).json() == updated.json()


async def test_unknown_category_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    other = await make_account(db_session)
    created = await db_client.post("/triage/categories", json={"name": "Own"})
    assert created.status_code == 201
    # Another user's own category is unknown here, as is a random ID.
    foreign = TriageCategory(owner_user_id=other.user.id, name="Foreign")
    db_session.add(foreign)
    await db_session.flush()
    for category_id in (uuid.uuid4(), foreign.id):
        response = await db_client.put(
            "/notifications/settings", json={"category_ids": [str(category_id)]}
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "unknown_category"
    own = await db_client.put(
        "/notifications/settings", json={"category_ids": [created.json()["id"]]}
    )
    assert own.status_code == 200


async def test_admin_can_switch_notifications_off(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _signed_in(db_client, db_session)
    await db_client.put("/notifications/settings", json={"enabled": True})
    settings.notifications = NotificationsSettings(enabled=False)
    body = (await db_client.get("/notifications/settings")).json()
    assert (body["available"], body["enabled"]) == (False, False)


async def test_message_content_follows_the_subject_setting(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    account = await _signed_in(db_client, db_session)
    mail = await account.message("Contract renewal", sender_name="Erika Muster")
    category_id = await builtin_category(db_session, "important")
    await save_result(db_session, mail.id, Decision(category_id, 1, TriageSource.LLM))

    response = await db_client.get(f"/notifications/messages/{mail.id}")
    assert response.status_code == 200
    assert response.json() == {
        "message_id": str(mail.id),
        "mailbox_id": str(account.mailbox.id),
        "sender": "Erika Muster",
        "category": {"id": str(category_id), "name": "Important", "builtin_key": "important"},
        "subject": None,
        "sound": False,
    }
    await db_client.put("/notifications/settings", json={"show_subject": True, "sound": True})
    body = (await db_client.get(f"/notifications/messages/{mail.id}")).json()
    assert (body["subject"], body["sound"]) == ("Contract renewal", True)


async def test_messages_of_others_are_not_found(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    other = await make_account(db_session)
    mail = await other.message()
    assert (await db_client.get(f"/notifications/messages/{mail.id}")).status_code == 404
    assert (await db_client.get(f"/notifications/messages/{uuid.uuid4()}")).status_code == 404

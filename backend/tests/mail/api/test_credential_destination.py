"""Stored mailbox credentials only go where they went before (#219).

A stolen session must not point a mailbox at its own server and receive the password with
the connection test: changing host, port or transport security (IMAP and SMTP) or the
token endpoint needs the credentials again. All names, hosts and passwords are invented.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MailSettings, Settings
from app.mail.models import Mailbox, MailboxType
from app.mail.providers.registry import ProviderRegistry, registry
from app.users.models import UserRole
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client
from tests.mail.api.conftest import IMAP_SETTINGS, PASSWORD, FakeServer, add_mailbox, mailbox_body

pytestmark = pytest.mark.db

ATTACKER = {**IMAP_SETTINGS, "host": "imap.attacker.example"}
NEW_DESTINATIONS = [
    {"host": "imap.attacker.example"},
    {"port": 1993},
    {"security": "starttls"},
    {"verify_certificate": False},
    {"smtp_host": "smtp.attacker.example"},
    {"smtp_port": 2525},
    {"smtp_security": "tls"},
]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"mail": MailSettings(connection_test_max_attempts=3)})


@pytest.fixture
async def admin(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    async with api_client(app) as http:
        assert (await login(http, "admin@example.org")).status_code == 200
        yield http


async def _stored(db: AsyncSession, mailbox_id: object) -> Mailbox:
    mailbox = await db.get(Mailbox, uuid.UUID(str(mailbox_id)), populate_existing=True)
    assert mailbox is not None
    return mailbox


async def _shared_mailbox(admin: AsyncClient) -> str:
    response = await admin.post(
        "/admin/shared-mailboxes", json=mailbox_body(address="team@example.org")
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# -- registry -------------------------------------------------------------------------


def test_only_declared_keys_are_a_destination() -> None:
    providers = ProviderRegistry()
    providers.register(MailboxType.IMAP, lambda config: None, destination=("host",))  # type: ignore[arg-type,return-value]

    assert providers.destination_changed(MailboxType.IMAP, {"host": "a"}, {"host": "b"})
    assert not providers.destination_changed(
        MailboxType.IMAP, {"host": "a"}, {"host": "a", "username": "x"}
    )


def test_without_declaration_every_key_is_a_destination() -> None:
    providers = ProviderRegistry()
    providers.register(MailboxType.IMAP, lambda config: None)  # type: ignore[arg-type,return-value]

    assert providers.destination_changed(MailboxType.IMAP, {"a": 1}, {"a": 1, "b": 2})
    assert not providers.destination_changed(MailboxType.IMAP, {"a": 1}, {"a": 1})


@pytest.mark.parametrize("change", NEW_DESTINATIONS)
def test_imap_destination(change: dict[str, object]) -> None:
    assert registry.destination_changed(
        MailboxType.IMAP, IMAP_SETTINGS, {**IMAP_SETTINGS, **change}
    )


def test_oauth_destinations() -> None:
    assert registry.destination_changed(
        MailboxType.GRAPH, {"tenant_id": "contoso"}, {"tenant_id": "fabrikam"}
    )
    assert not registry.destination_changed(MailboxType.GRAPH, {}, {"user_id": "u1"})
    assert not registry.destination_changed(MailboxType.GMAIL, {}, {"include_spam_trash": True})


# -- own mailboxes --------------------------------------------------------------------


@pytest.mark.parametrize("change", NEW_DESTINATIONS)
async def test_new_destination_needs_the_credentials(
    erika: AsyncClient, server: FakeServer, db_session: AsyncSession, change: dict[str, object]
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]
    connections = len(server.connections)

    response = await erika.patch(
        f"/mailboxes/{mailbox_id}",
        json={"provider_settings": {**IMAP_SETTINGS, **change}, "display_name": "Changed"},
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "credentials_required"
    # No connection with the stored password, nothing saved.
    assert len(server.connections) == connections
    mailbox = await _stored(db_session, mailbox_id)
    assert mailbox.provider_settings == IMAP_SETTINGS
    assert mailbox.display_name == "erika@example.org"


async def test_same_destination_uses_the_stored_credentials(
    erika: AsyncClient, server: FakeServer
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]

    response = await erika.patch(
        f"/mailboxes/{mailbox_id}",
        json={"provider_settings": {**IMAP_SETTINGS, "smtp_save_sent": False}},
    )

    assert response.status_code == 200
    assert server.connections[-1].settings["host"] == IMAP_SETTINGS["host"]
    assert server.connections[-1].credentials == {"password": PASSWORD}


async def test_new_destination_with_new_credentials(
    erika: AsyncClient, server: FakeServer, db_session: AsyncSession
) -> None:
    mailbox_id = (await add_mailbox(erika))["id"]

    response = await erika.patch(
        f"/mailboxes/{mailbox_id}",
        json={"provider_settings": ATTACKER, "credentials": {"password": PASSWORD}},
    )

    assert response.status_code == 200
    assert server.connections[-1].settings == ATTACKER
    assert (await _stored(db_session, mailbox_id)).provider_settings == ATTACKER


# -- shared mailboxes -----------------------------------------------------------------


async def test_shared_mailbox_new_destination_needs_the_credentials(
    admin: AsyncClient, server: FakeServer, db_session: AsyncSession
) -> None:
    mailbox_id = await _shared_mailbox(admin)
    connections = len(server.connections)

    rejected = await admin.patch(
        f"/admin/shared-mailboxes/{mailbox_id}", json={"provider_settings": ATTACKER}
    )
    assert rejected.status_code == 422
    assert rejected.json()["error_code"] == "credentials_required"
    assert len(server.connections) == connections
    assert (await _stored(db_session, mailbox_id)).provider_settings == IMAP_SETTINGS

    same = await admin.patch(
        f"/admin/shared-mailboxes/{mailbox_id}",
        json={"provider_settings": {**IMAP_SETTINGS, "username": "team"}},
    )
    assert same.status_code == 200
    assert server.connections[-1].credentials == {"password": PASSWORD}

    moved = await admin.patch(
        f"/admin/shared-mailboxes/{mailbox_id}",
        json={"provider_settings": ATTACKER, "credentials": {"password": PASSWORD}},
    )
    assert moved.status_code == 200
    assert server.connections[-1].settings == ATTACKER


async def test_shared_mailbox_connection_tests_are_throttled(
    admin: AsyncClient, server: FakeServer
) -> None:
    mailbox_id = await _shared_mailbox(admin)
    body = {"credentials": {"password": "wrong"}}

    for _ in range(3):
        response = await admin.patch(f"/admin/shared-mailboxes/{mailbox_id}", json=body)
        assert response.json()["error_code"] == "authentication_failed"
    connections = len(server.connections)
    throttled = await admin.patch(f"/admin/shared-mailboxes/{mailbox_id}", json=body)

    assert throttled.status_code == 429
    assert len(server.connections) == connections

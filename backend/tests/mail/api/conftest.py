"""Fixtures for the mailbox API: an app on the rolled-back test session with a fake mail
server instead of real providers and a recorder instead of the job queue. All names,
addresses and passwords are invented."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import MailSettings, Settings, StorageSettings
from app.core.crypto import configure_keyring, set_keyring
from app.core.db import get_db
from app.mail.api.router import (
    get_deletion_requester,
    get_provider_registry,
    get_sync_requester,
)
from app.mail.models import FolderRole, MailboxType
from app.mail.providers.base import AuthenticationError, MailboxConfig, RemoteFolder
from app.mail.providers.fake import FakeMailProvider
from app.mail.providers.registry import ProviderRegistry
from app.mail.storage import AttachmentStorage
from app.mail.sync.engine import SyncStats, sync_mailbox
from app.main import create_app
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.mail.conftest import load_fixture

PASSWORD = "correct-horse-battery"
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
IMAP_SETTINGS = {"host": "imap.example.org", "port": 993, "security": "tls"}


@dataclass
class FakeServer:
    """One fake mail server for every mailbox; accepts only ``PASSWORD``."""

    provider: FakeMailProvider = field(default_factory=FakeMailProvider)
    connections: list[MailboxConfig] = field(default_factory=list)

    def factory(self, config: MailboxConfig) -> FakeMailProvider:
        self.connections.append(config)
        if config.credentials.get("password") != PASSWORD:
            raise AuthenticationError()
        return self.provider


@pytest.fixture
def server() -> FakeServer:
    server = FakeServer()
    server.provider.add_folder(RemoteFolder("INBOX", "Inbox", role=FolderRole.INBOX))
    server.provider.add_folder(RemoteFolder("Archive", "Archive", role=FolderRole.ARCHIVE))
    server.provider.add_folder(RemoteFolder("Trash", "Trash", role=FolderRole.TRASH))
    server.provider.add_message("INBOX", load_fixture("01-plain-ascii.eml"), received_at=NOW)
    server.provider.add_message("INBOX", load_fixture("05-nested-multipart.eml"), received_at=NOW)
    server.provider.add_message(
        "Archive", load_fixture("09-signature-delimiter.eml"), received_at=NOW
    )
    server.provider.add_message("Trash", load_fixture("04-newsletter-html.eml"), received_at=NOW)
    return server


@pytest.fixture
def sync_requests() -> list[uuid.UUID]:
    return []


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def storage(data_dir: Path) -> AttachmentStorage:
    return AttachmentStorage(data_dir)


@pytest.fixture
async def app(
    settings: Settings,
    db_session: AsyncSession,
    server: FakeServer,
    sync_requests: list[uuid.UUID],
    data_dir: Path,
) -> AsyncIterator[FastAPI]:
    settings = settings.model_copy(
        update={"storage": StorageSettings.model_validate({"data_dir": data_dir})}
    )
    app = create_app(settings)
    # What the lifespan does (ASGITransport does not run it): credentials are encrypted.
    configure_keyring(settings.security)
    providers = ProviderRegistry()
    providers.register(MailboxType.IMAP, server.factory)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def record_sync(mailbox_id: uuid.UUID) -> bool:
        sync_requests.append(mailbox_id)
        return True

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_provider_registry] = lambda: providers
    app.dependency_overrides[get_sync_requester] = lambda: record_sync

    # Queued removals (``deletion_requests``); the jobs do not run.
    app.state.deletion_requests = []

    async def record_deletion(mailbox_id: uuid.UUID) -> bool:
        app.state.deletion_requests.append(mailbox_id)
        return True

    app.dependency_overrides[get_deletion_requester] = lambda: record_deletion
    yield app
    set_keyring(None)
    await app.state.database.dispose()


@pytest.fixture
def deletion_requests(app: FastAPI) -> list[uuid.UUID]:
    requests: list[uuid.UUID] = app.state.deletion_requests
    return requests


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_client(app) as http:
        yield http


@pytest.fixture
async def erika(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        assert (await login(http, "erika@example.org")).status_code == 200
        yield http


@pytest.fixture
async def bob(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "bob@example.org")
    async with api_client(app) as http:
        assert (await login(http, "bob@example.org")).status_code == 200
        yield http


def mailbox_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "type": "imap",
        "address": "erika@example.org",
        "provider_settings": IMAP_SETTINGS,
        "credentials": {"password": PASSWORD},
    }
    body.update(overrides)
    return body


async def add_mailbox(client: AsyncClient, **overrides: object) -> dict[str, object]:
    response = await client.post("/mailboxes", json=mailbox_body(**overrides))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def run_sync(
    session: AsyncSession,
    mailbox_id: uuid.UUID | str,
    server: FakeServer,
    storage: AttachmentStorage,
) -> SyncStats | None:
    """What the worker's sync job does, with the fake server."""
    return await sync_mailbox(
        session,
        uuid.UUID(str(mailbox_id)),
        storage=storage,
        settings=MailSettings(),
        provider_factory=server.factory,
        now=lambda: NOW,
    )

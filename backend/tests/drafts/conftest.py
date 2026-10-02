"""Fixtures for reply draft tests: signed-in clients, synthetic threads, a fake chat model
and a fake mail provider that records what is sent.

All names, addresses and texts are invented (example.* domains, docs/PRIVACY.md).
"""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import get_llm
from app.core.config import DraftsSettings, Settings
from app.core.db import get_db
from app.core.ids import uuid7
from app.drafts.router import get_session_factory
from app.mail.api.router import get_provider_registry
from app.mail.models import (
    Folder,
    FolderRole,
    Mailbox,
    MailboxAssignment,
    MailboxType,
    Message,
    Thread,
)
from app.mail.providers.base import MailboxConfig
from app.mail.providers.fake import FakeMailProvider
from app.mail.providers.registry import ProviderRegistry
from app.main import create_app
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.rag.conftest import FakeLLM, fake_llm, session_factory  # noqa: F401

BASE = datetime(2026, 9, 28, 9, 0, tzinfo=UTC)


@dataclass
class Outbox:
    """Fake providers handed out by the registry, one per send."""

    providers: list[FakeMailProvider] = field(default_factory=list)
    configs: list[MailboxConfig] = field(default_factory=list)
    error: Exception | None = None

    def create(self, config: MailboxConfig) -> FakeMailProvider:
        provider = FakeMailProvider(config)
        if self.error is not None:
            provider.send_error = self.error  # type: ignore[assignment]
        self.providers.append(provider)
        self.configs.append(config)
        return provider

    @property
    def sent(self) -> list[Any]:
        return [reply for provider in self.providers for reply in provider.sent]


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
def draft_settings() -> Settings:
    return Settings(drafts=DraftsSettings(thread_messages=3, style_examples=2))


@pytest.fixture
async def app(
    draft_settings: Settings,
    settings: Settings,
    db_session: AsyncSession,
    fake_llm: FakeLLM,  # noqa: F811
    outbox: Outbox,
) -> AsyncIterator[FastAPI]:
    merged = settings.model_copy(update={"drafts": draft_settings.drafts})
    application = create_app(merged)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    registry = ProviderRegistry()
    for mailbox_type in MailboxType:
        registry.register(mailbox_type, outbox.create)
    application.dependency_overrides[get_db] = override_get_db
    application.dependency_overrides[get_llm] = lambda: fake_llm.gateway
    application.dependency_overrides[get_session_factory] = lambda: session_factory(db_session)
    application.dependency_overrides[get_provider_registry] = lambda: registry
    yield application
    await application.state.database.dispose()


Client = Callable[[str], Awaitable[tuple[AsyncClient, User]]]


@pytest.fixture
async def signed_in(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[Client]:
    """``await signed_in("erika@example.org")``: a new local user and a client signed in."""
    clients: list[AsyncClient] = []

    async def make(email: str) -> tuple[AsyncClient, User]:
        user = await make_local_user(db_session, email)
        client = api_client(app)
        clients.append(client)
        assert (await login(client, email)).status_code == 200
        return client, user

    yield make
    for client in clients:
        await client.aclose()


@dataclass
class Mails:
    session: AsyncSession

    async def mailbox(
        self, owner: User | None, address: str = "erika@example.org", **values: Any
    ) -> Mailbox:
        mailbox = Mailbox(
            type=values.pop("type", MailboxType.IMAP),
            display_name=values.pop("display_name", "Work"),
            address=address,
            owner_user_id=owner.id if owner else None,
            is_shared=owner is None,
            **values,
        )
        self.session.add(mailbox)
        await self.session.flush()
        return mailbox

    async def assign(self, mailbox: Mailbox, user: User) -> MailboxAssignment:
        assignment = MailboxAssignment(mailbox_id=mailbox.id, user_id=user.id)
        self.session.add(assignment)
        await self.session.flush()
        return assignment

    async def folder(self, mailbox: Mailbox, role: FolderRole, name: str) -> Folder:
        folder = Folder(mailbox_id=mailbox.id, remote_id=name, name=name, role=role)
        self.session.add(folder)
        await self.session.flush()
        return folder

    async def thread(self, mailbox: Mailbox, provider_thread_id: str | None = None) -> Thread:
        thread = Thread(
            mailbox_id=mailbox.id, provider_thread_id=provider_thread_id, subject_key="offer"
        )
        self.session.add(thread)
        await self.session.flush()
        return thread

    async def message(
        self,
        mailbox: Mailbox,
        body: str,
        *,
        thread: Thread | None = None,
        subject: str = "Angebot",
        sender: dict[str, Any] | None = None,
        to: Sequence[dict[str, Any]] | None = None,
        cc: Sequence[dict[str, Any]] = (),
        minutes: int = 0,
        language: str | None = "de",
        folders: Sequence[Folder] = (),
        references: Sequence[str] = (),
    ) -> Message:
        message = Message(
            id=uuid7(),
            mailbox_id=mailbox.id,
            thread_id=thread.id if thread else None,
            remote_ref=uuid.uuid4().hex,
            message_id_header=f"<{uuid.uuid4().hex}@example.org>",
            references=list(references),
            subject=subject,
            sender=sender or {"name": "Max Mustermann", "address": "max@example.org"},
            to=list(to if to is not None else [{"name": None, "address": mailbox.address}]),
            cc=list(cc),
            body_text=body,
            body_main=body,
            language=language,
            sent_at=BASE + timedelta(minutes=minutes),
            received_at=BASE + timedelta(minutes=minutes),
            folders=list(folders),
        )
        self.session.add(message)
        await self.session.flush()
        return message


@pytest.fixture
def mails(db_session: AsyncSession) -> Mails:
    return Mails(db_session)


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def generate(client: AsyncClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    response = await client.post("/drafts/generate", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)

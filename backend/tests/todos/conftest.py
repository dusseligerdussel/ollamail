"""Fixtures for todo tests: synthetic mailboxes and mails, a scripted fake LLM.
All names and addresses are invented."""

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.core.config import LLMSettings
from app.mail.models import Mailbox, MailboxType, Message, Thread
from app.todos import extraction
from app.users.models import User
from tests.ai.fakes import FakeFactory, FakeProvider, RecordingSink
from tests.factories import make_user
from tests.processing.conftest import pipeline  # noqa: F401 (fixture)

# Wednesday, 7 October 2026, 23:30 UTC = Thursday 01:30 in Berlin.
LATE_WEDNESDAY_UTC = datetime(2026, 10, 7, 23, 30, tzinfo=UTC)
WEDNESDAY_MORNING = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)


@dataclass
class FakeLLM:
    gateway: LLMGateway
    provider: FakeProvider
    sink: RecordingSink

    def answer(
        self, todos: list[dict[str, Any]] | None = None, done: list[int] | None = None
    ) -> None:
        self.provider.answers.append(json.dumps({"todos": todos or [], "done": done or []}))

    def prompt(self, index: int = -1) -> str:
        return "\n".join(m.content for m in self.provider.calls[index].messages)


@pytest.fixture
def fake_llm() -> FakeLLM:
    provider = FakeProvider()
    sink = RecordingSink()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b")),
        provider_factory=FakeFactory(default=provider),
        metrics=sink,
    )
    return FakeLLM(gateway, provider, sink)


@pytest.fixture(autouse=True)
def _no_triage() -> Iterator[None]:
    yield
    extraction.set_category_lookup(None)


@dataclass
class MailData:
    session: AsyncSession
    user: User
    mailbox: Mailbox

    async def thread(self) -> Thread:
        thread = Thread(mailbox_id=self.mailbox.id, subject_key="quarterly report")
        self.session.add(thread)
        await self.session.flush()
        return thread

    async def message(
        self,
        body: str = "Please send me the quarterly report by next Friday.",
        *,
        thread: Thread | None = None,
        sender: str = "max@example.com",
        sent_at: datetime = WEDNESDAY_MORNING,
        language: str | None = "en",
        mailbox: Mailbox | None = None,
    ) -> Message:
        message = Message(
            mailbox_id=(mailbox or self.mailbox).id,
            thread_id=thread.id if thread else None,
            remote_ref=uuid.uuid4().hex,
            subject="Quarterly report",
            sender={"name": "Max Example", "address": sender},
            to=[{"name": None, "address": (mailbox or self.mailbox).address}],
            sent_at=sent_at,
            body_text=body,
            body_main=body,
            language=language,
        )
        self.session.add(message)
        await self.session.flush()
        return message


async def make_mailbox(session: AsyncSession, user: User | None, address: str) -> Mailbox:
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Work",
        address=address,
        owner_user_id=user.id if user else None,
        is_shared=user is None,
    )
    session.add(mailbox)
    await session.flush()
    return mailbox


@pytest.fixture
async def mail(db_session: AsyncSession) -> MailData:
    user = await make_user(db_session, display_name="Erika Example", timezone="Europe/Berlin")
    mailbox = await make_mailbox(db_session, user, "erika@example.org")
    return MailData(db_session, user, mailbox)

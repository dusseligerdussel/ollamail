"""Fixtures for triage tests: users with mailboxes, messages and a gateway with a fake LLM.

All mail data is synthetic.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.core.config import LLMSettings, TriageSettings
from app.mail.models import Folder, FolderRole, Mailbox, MailboxType, Message
from app.users.models import User, UserRole
from tests.ai.fakes import FakeFactory, FakeProvider
from tests.factories import make_user

NOW = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def answer(category: str, priority: int = 2, reason: str = "Test reason.") -> str:
    """A model answer in the triage schema."""
    return json.dumps({"category": category, "priority": priority, "reason": reason})


@dataclass
class FakeLLM:
    gateway: LLMGateway
    provider: FakeProvider

    def answer(self, *answers: str | Exception) -> None:
        self.provider.answers.extend(answers)

    def prompts(self) -> list[str]:
        """System and user texts of every chat call, joined per call."""
        return ["\n".join(m.content for m in call.messages) for call in self.provider.calls]


@pytest.fixture
def fake_llm() -> FakeLLM:
    provider = FakeProvider()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings()), provider_factory=FakeFactory(default=provider)
    )
    return FakeLLM(gateway, provider)


@pytest.fixture
def triage_settings() -> TriageSettings:
    return TriageSettings(few_shot_examples=2, few_shot_embeddings=False)


@dataclass
class Account:
    """A user with one mailbox and its inbox folder."""

    session: AsyncSession
    user: User
    mailbox: Mailbox
    inbox: Folder
    counter: list[int] = field(default_factory=lambda: [0])

    async def message(
        self,
        subject: str = "Quarterly planning",
        *,
        sender: str = "colleague@example.org",
        sender_name: str | None = "Max Muster",
        body: str = "Can we meet on Thursday to plan the next quarter?",
        headers: Sequence[tuple[str, str]] = (),
        cc_me: bool = False,
        in_inbox: bool = True,
    ) -> Message:
        self.counter[0] += 1
        own = {"name": None, "address": self.mailbox.address}
        message = Message(
            mailbox_id=self.mailbox.id,
            remote_ref=f"ref-{uuid.uuid4().hex}",
            subject=subject,
            sender={"name": sender_name, "address": sender},
            to=[] if cc_me else [own],
            cc=[own] if cc_me else [],
            headers=[[name, value] for name, value in headers],
            body_text=body,
            body_main=body,
            received_at=NOW + timedelta(minutes=self.counter[0]),
            folders=[self.inbox] if in_inbox else [],
        )
        self.session.add(message)
        await self.session.flush()
        return message


async def make_account(
    session: AsyncSession, *, role: UserRole = UserRole.USER, language: str = "en", **user: Any
) -> Account:
    owner = await make_user(session, role=role, language=language, **user)
    return await account_for(session, owner)


async def account_for(session: AsyncSession, owner: User) -> Account:
    """A mailbox with an inbox folder for an existing user."""
    mailbox = Mailbox(
        type=MailboxType.IMAP,
        display_name="Test",
        address=f"inbox-{uuid.uuid4().hex[:8]}@example.org",
        owner_user_id=owner.id,
    )
    session.add(mailbox)
    await session.flush()
    inbox = Folder(mailbox_id=mailbox.id, remote_id="INBOX", name="INBOX", role=FolderRole.INBOX)
    session.add(inbox)
    await session.flush()
    return Account(session, owner, mailbox, inbox)


@pytest.fixture
async def account(db_session: AsyncSession) -> Account:
    return await make_account(db_session)


@pytest.fixture
async def other_account(db_session: AsyncSession) -> Account:
    return await make_account(db_session)

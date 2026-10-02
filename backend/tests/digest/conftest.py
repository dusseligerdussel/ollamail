"""Fixtures for digest tests: a fake LLM that answers map, condense and reduce prompts, a
fake TTS that writes small audio files, synthetic users, mailboxes and mails. All names
and addresses are invented."""

import json
import re
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, Usage
from app.ai.tts import AudioFile, AudioFormat, TTSService
from app.ai.tts.encode import FFmpegEncoder
from app.core.config import DigestSettings, LLMSettings, TTSSettings
from app.mail.models import Folder, FolderRole, Mailbox, MailboxType, Message
from app.triage.models import TriageCategory, TriageResult, TriageSource
from app.users.models import User
from tests.ai.fakes import Call, FakeFactory, FakeProvider, RecordingSink
from tests.ai.tts.fakes import FakeEngine
from tests.factories import make_user

MAIL_REF = re.compile(r"^\[(\d+)\] From:", re.MULTILINE)
NOTE_REF = re.compile(r"\[(\d+(?:, \d+)*)\]")


def _refs(text: str) -> list[int]:
    return sorted({int(n) for group in NOTE_REF.findall(text) for n in group.split(", ")})


@dataclass
class DigestLLM(FakeProvider):
    """Answers by prompt type. Map calls containing one of ``broken_refs`` get invalid JSON;
    ``reduce_answer`` replaces the default reduce answer."""

    broken_refs: set[int] = field(default_factory=set)
    reduce_answer: str | None = None
    kinds: list[str] = field(default_factory=list)

    @staticmethod
    def kind(messages: Sequence[ChatMessage]) -> str:
        system = messages[0].content
        if "deadline: " in system:
            return "map"
        if "Merge these notes" in system or "Fasse diese Notizen" in system:
            return "condense"
        return "reduce"

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        self.calls.append(Call(list(messages), model, schema, options))
        kind = self.kind(messages)
        self.kinds.append(kind)
        user = next(m.content for m in messages if m.role == "user")
        if kind == "map":
            refs = [int(ref) for ref in MAIL_REF.findall(user)]
            if self.broken_refs & set(refs):
                content = "not json"
            else:
                items = [
                    {"ref": ref, "summary": f"Summary of mail {ref}.", "deadline": None}
                    for ref in refs
                ]
                content = json.dumps({"items": items})
        elif kind == "condense":
            lines = [line for line in user.splitlines() if line.strip()]
            refs = ", ".join(str(ref) for ref in _refs(user))
            content = f"Condensed {len(lines)} notes [{refs}]"
        elif self.reduce_answer is not None:
            content = self.reduce_answer
        else:
            refs = ", ".join(str(ref) for ref in _refs(user))
            content = f"The mails are summarised here [{refs}]."
        return LLMResult(content=content, model=model, usage=Usage(10, 5))


@dataclass
class FakeLLM:
    gateway: LLMGateway
    provider: DigestLLM
    sink: RecordingSink


def make_llm(context_tokens: int = 8192) -> FakeLLM:
    provider = DigestLLM()
    sink = RecordingSink()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b", context_tokens=context_tokens)),
        provider_factory=FakeFactory(default=provider),
        metrics=sink,
    )
    return FakeLLM(gateway, provider, sink)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return make_llm()


class FileEncoder(FFmpegEncoder):
    """Writes the PCM bytes as "audio" file per format, so files exist with known sizes."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[Path] = []

    async def encode(
        self,
        pcm: AsyncIterator[bytes],
        *,
        sample_rate: int,
        target: Path,
        formats: Sequence[AudioFormat],
    ) -> list[AudioFile]:
        data = b"".join([chunk async for chunk in pcm])
        target.parent.mkdir(parents=True, exist_ok=True)
        files = []
        for fmt in formats:
            path = target.with_name(target.name + fmt.extension)
            path.write_bytes(data)
            files.append(AudioFile(path, fmt, len(data) / 2 / sample_rate, len(data)))
        return files


@dataclass
class FakeTTS:
    service: TTSService
    engine: FakeEngine


def make_tts() -> FakeTTS:
    engine = FakeEngine(installed={"de_DE-thorsten-medium", "en_US-ljspeech-medium"})
    service = TTSService(engine, FileEncoder(), TTSSettings(sentence_pause=0, paragraph_pause=0))
    return FakeTTS(service, engine)


@pytest.fixture
def fake_tts() -> FakeTTS:
    return make_tts()


@pytest.fixture
def config() -> DigestSettings:
    return DigestSettings()


# -- data --------------------------------------------------------------------------------


async def make_mailbox(session: AsyncSession, user: User, address: str) -> Mailbox:
    mailbox = Mailbox(
        type=MailboxType.IMAP, display_name="Work", address=address, owner_user_id=user.id
    )
    session.add(mailbox)
    await session.flush()
    for role in (FolderRole.INBOX, FolderRole.SENT, FolderRole.JUNK):
        session.add(Folder(mailbox_id=mailbox.id, remote_id=role.value, name=role.value, role=role))
    await session.flush()
    return mailbox


async def folder(session: AsyncSession, mailbox: Mailbox, role: FolderRole) -> Folder:
    await session.refresh(mailbox, ["folders"])
    return next(f for f in mailbox.folders if f.role == role)


async def builtin_category(session: AsyncSession, key: str) -> TriageCategory:
    from sqlalchemy import select

    category = await session.scalar(
        select(TriageCategory).where(
            TriageCategory.builtin_key == key, TriageCategory.owner_user_id.is_(None)
        )
    )
    assert category is not None
    return category


@dataclass
class DigestData:
    session: AsyncSession
    user: User
    mailbox: Mailbox

    async def mail(
        self,
        subject: str = "Quarterly report",
        *,
        received_at: datetime,
        sender: str = "Max Example",
        body: str = "Please send me the quarterly report by Friday.",
        category: str | None = None,
        priority: int = 2,
        role: FolderRole = FolderRole.INBOX,
        mailbox: Mailbox | None = None,
    ) -> Message:
        box = mailbox or self.mailbox
        message = Message(
            mailbox_id=box.id,
            remote_ref=uuid.uuid4().hex,
            subject=subject,
            sender={"name": sender, "address": "max@example.com"},
            received_at=received_at,
            sent_at=received_at,
            body_text=body,
            body_main=body,
            language="en",
        )
        message.folders.append(await folder(self.session, box, role))
        self.session.add(message)
        await self.session.flush()
        if category is not None:
            self.session.add(
                TriageResult(
                    message_id=message.id,
                    category_id=(await builtin_category(self.session, category)).id,
                    priority=priority,
                    source=TriageSource.LLM,
                )
            )
            await self.session.flush()
        return message


@pytest.fixture
async def data(db_session: AsyncSession) -> DigestData:
    user = await make_user(
        db_session, display_name="Erika Example", timezone="Europe/Berlin", language="de"
    )
    mailbox = await make_mailbox(db_session, user, "erika@example.org")
    return DigestData(db_session, user, mailbox)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]

"""Fixtures for RAG tests: a fake chat model, indexed synthetic mails, the service.

All names, addresses and texts are invented (example.* domains, docs/PRIVACY.md).
"""

import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import ChatMessage, EnvConfigResolver, GenerationOptions, LLMGateway, LLMResult
from app.core.config import LLMSettings, RagSettings, SearchSettings, Settings
from app.mail.storage import AttachmentStorage
from app.rag.rerank import LLMReranker, Ranking
from app.rag.service import RagEvent, RagService, SessionFactory
from app.search.service import index_message
from tests.ai.fakes import RecordingSink
from tests.search.conftest import (  # noqa: F401 (fixtures)
    FakeEmbedder,
    MailData,
    embedder,
    mail,
    search_settings,
    storage,
)

_SOURCE_NUMBERS = re.compile(r'<mail-[0-9a-f]+ n="(\d+)">')

Answer = str | Exception | Callable[[list[ChatMessage]], str]


@dataclass
class Call:
    messages: list[ChatMessage]
    schema: type[BaseModel] | None

    @property
    def text(self) -> str:
        return "\n".join(m.content for m in self.messages)


def source_numbers(messages: Sequence[ChatMessage]) -> list[int]:
    return [int(n) for n in _SOURCE_NUMBERS.findall(messages[-1].content)]


@dataclass
class FakeChatModel:
    """Answers structured calls by schema (query analysis, ranking) and streams answers.

    ``answer`` is the streamed text or a function of the prompt; it is streamed in small
    pieces so that citation markers are split across chunks.
    """

    analysis: dict[str, Any] | Exception = field(default_factory=dict)
    ranking: list[int] | Exception = field(default_factory=list)
    answer: Answer = "Nothing [1]."
    piece: int = 3
    calls: list[Call] = field(default_factory=list)

    @property
    def streams(self) -> list[Call]:
        return [c for c in self.calls if c.schema is None]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        self.calls.append(Call(list(messages), schema))
        if schema is Ranking:
            value: Any = self.ranking
            if isinstance(value, Exception):
                raise value
            return LLMResult(content=json.dumps({"ranking": value}), model=model)
        if isinstance(self.analysis, Exception):
            raise self.analysis
        return LLMResult(content=json.dumps(self.analysis), model=model)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append(Call(list(messages), None))
        answer = self.answer
        if isinstance(answer, Exception):
            raise answer
        text = answer(list(messages)) if callable(answer) else answer
        for start in range(0, len(text), self.piece):
            yield text[start : start + self.piece]

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        # Wrong length on purpose: the search falls back to full text unless a test
        # passes the ``FakeEmbedder`` of the search tests.
        return [[1.0, 0.0] for _ in texts]

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        pass


@dataclass
class FakeLLM:
    gateway: LLMGateway
    model: FakeChatModel
    sink: RecordingSink


@pytest.fixture
def fake_llm() -> FakeLLM:
    model = FakeChatModel()
    sink = RecordingSink()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b", structured_output_retries=0)),
        provider_factory=lambda _: model,
        metrics=sink,
    )
    return FakeLLM(gateway, model, sink)


@pytest.fixture
def rag_settings(search_settings: SearchSettings) -> Settings:  # noqa: F811
    return Settings(search=search_settings, rag=RagSettings(retrieval_limit=5))


def session_factory(session: AsyncSession) -> SessionFactory:
    """Sessions for the code under test: always the rolled-back test session."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


@pytest.fixture
def make_service(
    db_session: AsyncSession,
    fake_llm: FakeLLM,
    rag_settings: Settings,
    embedder: FakeEmbedder,  # noqa: F811
) -> Callable[..., RagService]:
    def make(*, rerank: bool = False, settings: Settings | None = None) -> RagService:
        return RagService(
            fake_llm.gateway,
            session_factory(db_session),
            settings or rag_settings,
            embedder=embedder,
            reranker=LLMReranker(fake_llm.gateway) if rerank else None,
        )

    return make


@dataclass
class Inbox:
    """Mail data plus indexing: ``add`` stores a mail and indexes it."""

    mail: MailData
    embedder: FakeEmbedder
    storage: AttachmentStorage
    settings: SearchSettings

    async def add(
        self,
        mailbox_id: uuid.UUID,
        body: str,
        *,
        subject: str = "",
        sender: dict[str, Any] | None = None,
        sent_at: datetime | None = None,
        language: str | None = "en",
    ) -> uuid.UUID:
        message_id = await self.mail.message(
            mailbox_id, body, subject=subject, sender=sender, sent_at=sent_at, language=language
        )
        await index_message(
            self.mail.session,
            message_id,
            embedder=self.embedder,
            storage=self.storage,
            settings=self.settings,
        )
        return message_id


@pytest.fixture
def inbox(
    mail: MailData,  # noqa: F811
    embedder: FakeEmbedder,  # noqa: F811
    storage: AttachmentStorage,  # noqa: F811
    search_settings: SearchSettings,  # noqa: F811
) -> Inbox:
    return Inbox(mail, embedder, storage, search_settings)


async def collect(events: AsyncIterator[RagEvent]) -> list[RagEvent]:
    return [event async for event in events]


def answer_text(events: Sequence[RagEvent]) -> str:
    return "".join(e.text for e in events if e.type == "token")  # type: ignore[union-attr]

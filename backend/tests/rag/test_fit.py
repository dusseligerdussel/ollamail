"""Unit tests: sources are cut to the context window (no database)."""

import uuid

from app.ai.llm import ChatMessage, EnvConfigResolver, LLMGateway
from app.core.config import LLMSettings, RagSettings, Settings
from app.rag.service import MIN_SOURCE_CHARS, RagService
from app.search.service import SearchHit


def hit(content: str) -> SearchHit:
    return SearchHit(
        chunk_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        mailbox_id=uuid.uuid4(),
        attachment_id=None,
        source="body",
        heading="From: Erika Example",
        content=content,
        score=1.0,
        text_rank=1,
        vector_rank=None,
    )


def service(max_answer_tokens: int) -> RagService:
    settings = Settings(rag=RagSettings(max_answer_tokens=max_answer_tokens))
    gateway = LLMGateway(EnvConfigResolver(LLMSettings()))

    def no_sessions() -> object:
        raise AssertionError("no database access expected")

    return RagService(gateway, no_sessions, settings, embedder=None)  # type: ignore[arg-type]


def test_keeps_the_best_chunks_that_fit() -> None:
    hits = [hit("a" * 1000), hit("b" * 1000), hit("c" * 1000)]
    prompt = [ChatMessage(role="system", content="x" * 200)]

    # (1024 - 512) tokens * 3 chars - 200 chars of prompt: room for one chunk and a bit.
    fitted = service(512)._fit(hits, prompt, context_tokens=1024)

    assert fitted == hits[:1]


def test_shortens_the_best_chunk_if_nothing_fits() -> None:
    hits = [hit("a" * 3000), hit("b" * 10)]
    prompt = [ChatMessage(role="system", content="x" * 1000)]

    (fitted,) = service(512)._fit(hits, prompt, context_tokens=1024)

    assert fitted.chunk_id == hits[0].chunk_id
    assert MIN_SOURCE_CHARS <= len(fitted.content) < 1536 - 1000


def test_without_room_the_best_chunk_keeps_a_minimum() -> None:
    prompt = [ChatMessage(role="system", content="x" * 5000)]

    (fitted,) = service(512)._fit([hit("a" * 3000)], prompt, context_tokens=1024)

    assert len(fitted.content) == MIN_SOURCE_CHARS

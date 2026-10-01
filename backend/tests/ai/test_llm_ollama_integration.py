"""Against a real Ollama with a very small model; skipped if none is reachable.

    OLLAMAIL_TEST_OLLAMA_URL=http://localhost:11434 \
    OLLAMAIL_TEST_OLLAMA_MODEL=qwen2.5:0.5b uv run pytest -m ollama
"""

import os
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import BaseModel

from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.ollama import normalize_model_name
from app.ai.llm.types import ChatMessage, LLMTask
from app.core.config import LLMSettings

OLLAMA_URL = os.environ.get("OLLAMAIL_TEST_OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("OLLAMAIL_TEST_OLLAMA_MODEL", "qwen2.5:0.5b")

pytestmark = pytest.mark.ollama


class Sentiment(BaseModel):
    positive: bool


@pytest.fixture
async def gateway() -> AsyncIterator[LLMGateway]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            tags = (await client.get(f"{OLLAMA_URL}/api/tags")).json()
    except (httpx.HTTPError, ValueError):
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL}")
    installed = {normalize_model_name(m["name"]) for m in tags.get("models", [])}
    if normalize_model_name(MODEL) not in installed:
        pytest.skip(f"model {MODEL} not installed (ollama pull {MODEL})")
    settings = LLMSettings(
        base_url=OLLAMA_URL, default_chat_model=MODEL, context_tokens=2048, timeout=120
    )
    llm = LLMGateway(EnvConfigResolver(settings))
    yield llm
    await llm.aclose()


async def test_structured_output_against_ollama(gateway: LLMGateway) -> None:
    messages = [
        ChatMessage(role="system", content="Rate the sentiment of the user's message."),
        ChatMessage(role="user", content="I love this, thank you so much!"),
    ]

    result = await gateway.complete_structured(LLMTask.TRIAGE, messages, Sentiment)

    assert isinstance(result, Sentiment)


async def test_stream_against_ollama(gateway: LLMGateway) -> None:
    messages = [ChatMessage(role="user", content="Say hello.")]

    chunks = [chunk async for chunk in gateway.stream(LLMTask.RAG_CHAT, messages)]

    assert "".join(chunks).strip()

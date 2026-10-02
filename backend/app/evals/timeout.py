"""A time limit per model call for evaluation runs.

Some models generate without end for some prompts (e.g. JSON that never closes) until
the endpoint's HTTP timeout. In an evaluation that costs minutes per call and hides the
failure among other "unavailable" errors. :class:`TimeLimitedProvider` wraps a provider:
a call (a whole stream included) that exceeds ``seconds`` is cancelled, which closes the
connection so the server stops generating, and raises :class:`CallTimeoutError`. That is
an ``LLMUnavailableError`` for the features, and the gateway records its type name in the
call metrics, so the report counts timeouts per task.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence

from pydantic import BaseModel

from app.ai.llm import ChatMessage, GenerationOptions, LLMResult, LLMUnavailableError
from app.ai.llm.base import LLMProvider
from app.ai.llm.config import EndpointConfig
from app.ai.llm.gateway import ProviderFactory

TIMEOUT_ERROR = "CallTimeoutError"


class CallTimeoutError(LLMUnavailableError):
    """A model call took longer than the evaluation's time limit."""


class TimeLimitedProvider:
    def __init__(self, provider: LLMProvider, seconds: float) -> None:
        self._provider = provider
        self._seconds = seconds

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        try:
            async with asyncio.timeout(self._seconds):
                return await self._provider.complete(
                    messages, model=model, schema=schema, options=options
                )
        except TimeoutError as exc:
            raise CallTimeoutError(f"no answer within {self._seconds:g} s") from exc

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        try:
            async with asyncio.timeout(self._seconds):
                async for chunk in self._provider.stream(messages, model=model, options=options):
                    yield chunk
        except TimeoutError as exc:
            raise CallTimeoutError(f"no answer within {self._seconds:g} s") from exc

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        return await self._provider.embed(texts, model=model)

    async def list_models(self) -> list[str]:
        return await self._provider.list_models()

    async def aclose(self) -> None:
        await self._provider.aclose()


def time_limited(factory: ProviderFactory, seconds: float | None) -> ProviderFactory:
    """``factory`` with a time limit per call; unchanged for ``None``."""
    if seconds is None:
        return factory

    def create(endpoint: EndpointConfig) -> LLMProvider:
        return TimeLimitedProvider(factory(endpoint), seconds)

    return create

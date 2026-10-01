"""The provider interface (docs/ARCHITECTURE.md §3.2).

Providers are thin protocol adapters: they translate messages and the JSON schema into the
endpoint's API and return raw text. Validation, retries, cloud policy, context limits and
metrics live in :class:`app.ai.llm.gateway.LLMGateway`, so new providers get them for free.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from pydantic import BaseModel

from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        """Generate one answer. With ``schema`` the endpoint is asked for matching JSON;
        the returned content is not validated here."""
        ...

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[str]:
        """Generate an answer as text chunks."""
        ...

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        """One embedding vector per input text, in input order."""
        ...

    async def list_models(self) -> list[str]:
        """Names of the models the endpoint can serve."""
        ...

    async def aclose(self) -> None: ...

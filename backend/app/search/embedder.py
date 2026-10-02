"""Embeddings for the index: batched and throttled, through the LLM gateway.

Index jobs run on the ``llm`` queue, whose parallelism is ``OLLAMAIL_LLM_CONCURRENCY``.
On top of that, texts are sent in batches of ``OLLAMAIL_SEARCH_EMBED_BATCH_SIZE`` with an
optional pause in between, so a large initial import does not monopolise a CPU-only host.
"""

import asyncio
from collections.abc import Sequence
from typing import Protocol

from app.ai.llm import LLMGateway, LLMTask
from app.core.config import SearchSettings


class EmbeddingDimensionError(Exception):
    """The model returned vectors of another length than the index column."""


class Embedder(Protocol):
    async def current_model(self) -> str:
        """Name of the model new embeddings are made with (stored next to each vector)."""
        ...

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        """One vector per text; ``model`` overrides the current model (querying an index
        that is still made of the previous model's vectors)."""
        ...


class GatewayEmbedder:
    def __init__(self, gateway: LLMGateway, settings: SearchSettings) -> None:
        self._gateway = gateway
        self._settings = settings

    async def current_model(self) -> str:
        return (await self._gateway.assignment(LLMTask.EMBEDDINGS)).model

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        size = self._settings.embed_batch_size
        vectors: list[list[float]] = []
        for start in range(0, len(texts), size):
            if start and self._settings.embed_pause_seconds:
                await asyncio.sleep(self._settings.embed_pause_seconds)
            vectors.extend(
                await self._gateway.embed(
                    texts[start : start + size], task=LLMTask.EMBEDDINGS, model=model
                )
            )
        return vectors


def check_dimensions(vectors: Sequence[Sequence[float]], dimensions: int) -> None:
    if any(len(vector) != dimensions for vector in vectors):
        raise EmbeddingDimensionError

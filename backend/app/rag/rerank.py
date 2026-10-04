"""Optional reranking of retrieved chunks before answering.

Off for the ``cpu`` hardware profile (an extra model call per question is too slow there),
on for GPU profiles; ``OLLAMAIL_RAG_RERANKER_ENABLED`` overrides the profile. The default
implementation asks the chat model for a ranking (listwise, structured output); other
rerankers (e.g. a cross-encoder) implement :class:`Reranker`.
"""

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from app.ai.llm import GenerationOptions, LLMError, LLMGateway, LLMTask
from app.core.config import Settings
from app.core.logging import get_logger
from app.rag.citations import DataBlock, data_tag, render_blocks
from app.rag.prompts import RAG_RERANK
from app.search.service import SearchHit

log = get_logger(__name__)

# Characters per chunk shown to the reranker: enough to judge relevance.
EXCERPT_CHARS = 600
# Answer limit: ``{"ranking": [...]}`` needs a few tokens per number (128 for the default
# 24 candidates).
RERANK_BASE_TOKENS = 32
RERANK_TOKENS_PER_HIT = 4


def reranker_enabled(settings: Settings) -> bool:
    if settings.rag.reranker_enabled is not None:
        return settings.rag.reranker_enabled
    return settings.llm.profile != "cpu"


class Reranker(Protocol):
    async def rerank(
        self, question: str, hits: Sequence[SearchHit], limit: int, *, language: str
    ) -> list[SearchHit]:
        """At most ``limit`` of ``hits``, most relevant first."""
        ...


class Ranking(BaseModel):
    ranking: list[int]


class LLMReranker:
    def __init__(self, llm: LLMGateway) -> None:
        self._llm = llm

    async def rerank(
        self, question: str, hits: Sequence[SearchHit], limit: int, *, language: str
    ) -> list[SearchHit]:
        if len(hits) < 2:
            return list(hits[:limit])
        tag = data_tag()
        blocks = [
            DataBlock(number, hit.heading, hit.content[:EXCERPT_CHARS])
            for number, hit in enumerate(hits, start=1)
        ]
        messages = RAG_RERANK.render(
            language,
            tag=tag,
            question=question,
            sources=render_blocks(tag, blocks, feature="rag_rerank"),
        )
        try:
            result = await self._llm.complete_structured(
                LLMTask.RAG_CHAT,
                messages,
                Ranking,
                prompt_version=RAG_RERANK.id,
                options=GenerationOptions(
                    max_tokens=RERANK_BASE_TOKENS + RERANK_TOKENS_PER_HIT * len(hits)
                ),
                language=language,
            )
        except LLMError as exc:
            # Keep the order of the search.
            log.warning("rag_rerank_failed", error_type=type(exc).__name__)
            return list(hits[:limit])
        order: list[int] = []
        for number in result.ranking:
            if 1 <= number <= len(hits) and number not in order:
                order.append(number)
        # Chunks the model left out keep their search order behind the ranked ones.
        order.extend(n for n in range(1, len(hits) + 1) if n not in order)
        return [hits[n - 1] for n in order[:limit]]

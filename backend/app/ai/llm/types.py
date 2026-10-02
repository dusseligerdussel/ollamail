"""Value types shared by all LLM providers."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class LLMTask(StrEnum):
    """Features that use an LLM; each can be assigned its own endpoint and model."""

    TRIAGE = "triage"
    TODOS = "todos"
    DIGEST = "digest"
    RAG_CHAT = "rag_chat"
    REPLY_DRAFT = "reply_draft"
    EMBEDDINGS = "embeddings"


class ChatMessage(BaseModel):
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    temperature: float | None = None
    # Upper bound for generated tokens.
    max_tokens: int | None = None
    # Context window to request (Ollama ``num_ctx``); ignored by OpenAI-compatible APIs.
    context_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResult:
    content: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None

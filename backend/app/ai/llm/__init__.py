"""LLM provider abstraction (docs/ARCHITECTURE.md §3.2)."""

from app.ai.llm.base import LLMProvider
from app.ai.llm.config import (
    EndpointConfig,
    EnvConfigResolver,
    LLMConfigResolver,
    ModelAssignment,
)
from app.ai.llm.errors import (
    CloudLLMDisabledError,
    LLMCircuitOpenError,
    LLMError,
    LLMNotReadyError,
    LLMOutputError,
    LLMRequestError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelNotAvailableError,
    StructuredOutputUnsupportedError,
)
from app.ai.llm.gateway import LLMGateway, create_provider, get_llm
from app.ai.llm.types import ChatMessage, GenerationOptions, LLMResult, LLMTask, Usage

__all__ = [
    "ChatMessage",
    "CloudLLMDisabledError",
    "EndpointConfig",
    "EnvConfigResolver",
    "GenerationOptions",
    "LLMCircuitOpenError",
    "LLMConfigResolver",
    "LLMError",
    "LLMGateway",
    "LLMNotReadyError",
    "LLMOutputError",
    "LLMProvider",
    "LLMRequestError",
    "LLMResult",
    "LLMTask",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "ModelAssignment",
    "ModelNotAvailableError",
    "StructuredOutputUnsupportedError",
    "Usage",
    "create_provider",
    "get_llm",
]

"""Which endpoint and model serve a task.

:class:`LLMConfigResolver` is the seam for admin settings in the database (#18): this
module provides :class:`EnvConfigResolver`, which reads ``OLLAMAIL_LLM_*`` and the
hardware profile; a DB-backed resolver can replace it without touching the gateway or
any feature.
"""

from dataclasses import dataclass
from typing import Protocol

from app.ai.llm.profiles import PROFILES
from app.ai.llm.types import LLMTask
from app.core.config import (
    LLMEndpointSettings,
    LLMProviderKind,
    LLMSettings,
    StructuredOutputMode,
)

DEFAULT_ENDPOINT = "default"


@dataclass(frozen=True, slots=True)
class EndpointConfig:
    name: str
    provider: LLMProviderKind
    base_url: str
    api_key: str | None = None
    is_cloud: bool = False
    structured_output: StructuredOutputMode = "native"
    timeout: float = 300.0

    def __repr__(self) -> str:
        # Never render the API key.
        return f"EndpointConfig(name={self.name!r}, provider={self.provider!r})"


@dataclass(frozen=True, slots=True)
class ModelAssignment:
    task: LLMTask
    endpoint: EndpointConfig
    model: str
    context_tokens: int


class LLMConfigResolver(Protocol):
    async def resolve(self, task: LLMTask) -> ModelAssignment: ...

    async def cloud_allowed(self) -> bool:
        """Global admin switch for endpoints marked as cloud."""
        ...

    async def structured_output_retries(self) -> int: ...


class EnvConfigResolver:
    """Resolution from environment variables and code defaults."""

    def __init__(self, settings: LLMSettings) -> None:
        self._settings = settings
        profile = PROFILES[settings.profile]
        self._chat_model = settings.default_chat_model or profile.chat_model
        self._embedding_model = settings.default_embedding_model or profile.embedding_model
        self._context_tokens = settings.context_tokens or profile.context_tokens
        self._endpoints = {DEFAULT_ENDPOINT: self._default_endpoint(settings)} | {
            name: self._endpoint(name, value, settings.timeout)
            for name, value in settings.endpoints.items()
        }

    @staticmethod
    def _default_endpoint(s: LLMSettings) -> EndpointConfig:
        return EndpointConfig(
            name=DEFAULT_ENDPOINT,
            provider=s.provider,
            base_url=s.base_url,
            api_key=s.api_key.get_secret_value() if s.api_key else None,
            is_cloud=s.is_cloud,
            structured_output=s.structured_output,
            timeout=s.timeout,
        )

    @staticmethod
    def _endpoint(name: str, e: LLMEndpointSettings, default_timeout: float) -> EndpointConfig:
        return EndpointConfig(
            name=name,
            provider=e.provider,
            base_url=e.base_url,
            api_key=e.api_key.get_secret_value() if e.api_key else None,
            is_cloud=e.is_cloud,
            structured_output=e.structured_output,
            timeout=e.timeout or default_timeout,
        )

    async def resolve(self, task: LLMTask) -> ModelAssignment:
        model: str | None = getattr(self._settings, f"task_{task.value}_model")
        endpoint: str | None = getattr(self._settings, f"task_{task.value}_endpoint")
        default_model = self._embedding_model if task is LLMTask.EMBEDDINGS else self._chat_model
        return ModelAssignment(
            task=task,
            endpoint=self._endpoints[endpoint or DEFAULT_ENDPOINT],
            model=model or default_model,
            context_tokens=self._context_tokens,
        )

    async def cloud_allowed(self) -> bool:
        return self._settings.cloud_enabled

    async def structured_output_retries(self) -> int:
        return self._settings.structured_output_retries

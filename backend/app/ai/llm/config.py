"""Which endpoint and model serve a task.

:class:`LLMConfigResolver` is the seam for admin settings: :class:`ResolvedConfig`
combines the environment (``OLLAMAIL_LLM_*``, hardware profile) with
:class:`AIOverrides` from the database. :class:`EnvConfigResolver` uses the environment
only; ``app.ai.settings.resolver.DbConfigResolver`` adds the admin settings (#18). The
gateway and the features do not know which one they use.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from app.ai.llm.profiles import PROFILES
from app.ai.llm.types import LLMTask
from app.core.config import (
    LLMEndpointSettings,
    LLMProfileName,
    LLMProviderKind,
    LLMSettings,
    StructuredOutputMode,
)

DEFAULT_ENDPOINT = "default"

# Built-in answer limits per task, used unless ``OLLAMAIL_LLM_TASK_<TASK>_MAX_TOKENS`` or
# ``OLLAMAIL_LLM_MAX_OUTPUT_TOKENS`` is set. Todos: ten todos with description fit.
TASK_MAX_TOKENS: dict[LLMTask, int] = {LLMTask.TODOS: 800}
# Tasks with long prompts or answers get a multiple of the profile's call timeout, unless
# ``OLLAMAIL_LLM_TASK_<TASK>_CALL_TIMEOUT`` or ``OLLAMAIL_LLM_CALL_TIMEOUT`` is set.
CALL_TIMEOUT_FACTORS: dict[LLMTask, float] = {
    LLMTask.DIGEST: 2.0,
    LLMTask.RAG_CHAT: 2.0,
    LLMTask.REPLY_DRAFT: 2.0,
}


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
    # Answer limit when the feature sets none (Ollama ``num_predict``, OpenAI ``max_tokens``).
    max_output_tokens: int = 1024
    # Seconds one call may take in total; ``None``: no deadline (embeddings).
    call_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class TaskOverride:
    """Admin choice for one task; ``None`` falls back to the environment."""

    endpoint: str | None = None
    model: str | None = None


@dataclass(frozen=True, slots=True)
class AIOverrides:
    """Settings stored by the admin; every ``None`` falls back to the environment."""

    endpoints: Mapping[str, EndpointConfig] = field(default_factory=dict)
    cloud_enabled: bool | None = None
    profile: LLMProfileName | None = None
    concurrency: int | None = None
    tasks: Mapping[LLMTask, TaskOverride] = field(default_factory=dict)


class LLMConfigResolver(Protocol):
    async def resolve(self, task: LLMTask) -> ModelAssignment: ...

    async def cloud_allowed(self) -> bool:
        """Global admin switch for endpoints marked as cloud."""
        ...

    async def structured_output_retries(self) -> int: ...


def env_endpoints(settings: LLMSettings) -> dict[str, EndpointConfig]:
    """Endpoints from the environment: ``default`` plus ``OLLAMAIL_LLM_ENDPOINTS``."""

    def endpoint(name: str, e: LLMEndpointSettings | LLMSettings) -> EndpointConfig:
        return EndpointConfig(
            name=name,
            provider=e.provider,
            base_url=e.base_url,
            api_key=e.api_key.get_secret_value() if e.api_key else None,
            is_cloud=e.is_cloud,
            structured_output=e.structured_output,
            timeout=e.timeout or settings.timeout,
        )

    return {DEFAULT_ENDPOINT: endpoint(DEFAULT_ENDPOINT, settings)} | {
        name: endpoint(name, value) for name, value in settings.endpoints.items()
    }


class ResolvedConfig:
    """Effective configuration: admin overrides beat the environment, which beats the
    hardware profile. Environment endpoints win over stored ones of the same name."""

    def __init__(self, settings: LLMSettings, overrides: AIOverrides | None = None) -> None:
        overrides = overrides or AIOverrides()
        self.settings = settings
        self.overrides = overrides
        self.env_endpoints = env_endpoints(settings)
        self.endpoints = dict(overrides.endpoints) | self.env_endpoints
        self.profile: LLMProfileName = overrides.profile or settings.profile
        profile = PROFILES[self.profile]
        self.chat_model = settings.default_chat_model or profile.chat_model
        self.embedding_model = settings.default_embedding_model or profile.embedding_model
        self.context_tokens = settings.context_tokens or profile.context_tokens
        self.cloud_enabled = (
            settings.cloud_enabled if overrides.cloud_enabled is None else overrides.cloud_enabled
        )
        self.concurrency = overrides.concurrency or settings.concurrency
        self.call_timeout = settings.call_timeout or profile.call_timeout

    def env_task(self, task: LLMTask) -> TaskOverride:
        """Model and endpoint the environment assigns to ``task``, if any."""
        return TaskOverride(
            endpoint=getattr(self.settings, f"task_{task.value}_endpoint"),
            model=getattr(self.settings, f"task_{task.value}_model"),
        )

    def max_output_tokens(self, task: LLMTask) -> int:
        """Answer limit: task setting → global setting (if set) → built-in task default."""
        configured: int | None = getattr(self.settings, f"task_{task.value}_max_tokens", None)
        if configured is not None:
            return configured
        if "max_output_tokens" in self.settings.model_fields_set:
            return self.settings.max_output_tokens
        return TASK_MAX_TOKENS.get(task, self.settings.max_output_tokens)

    def task_call_timeout(self, task: LLMTask) -> float | None:
        """Deadline per call: task setting → global setting (if set) → profile times task
        factor. Embeddings have none (bounded by the HTTP timeout)."""
        if task is LLMTask.EMBEDDINGS:
            return None
        configured: float | None = getattr(self.settings, f"task_{task.value}_call_timeout", None)
        if configured is not None:
            return configured
        if self.settings.call_timeout is not None:
            return self.settings.call_timeout
        return self.call_timeout * CALL_TIMEOUT_FACTORS.get(task, 1.0)

    def default_model(self, task: LLMTask) -> str:
        return self.embedding_model if task is LLMTask.EMBEDDINGS else self.chat_model

    def assignment(self, task: LLMTask) -> ModelAssignment:
        stored = self.overrides.tasks.get(task, TaskOverride())
        env = self.env_task(task)
        endpoint = stored.endpoint if stored.endpoint in self.endpoints else None
        return ModelAssignment(
            task=task,
            endpoint=self.endpoints[endpoint or env.endpoint or DEFAULT_ENDPOINT],
            model=stored.model or env.model or self.default_model(task),
            context_tokens=self.context_tokens,
            max_output_tokens=self.max_output_tokens(task),
            call_timeout=self.task_call_timeout(task),
        )


class EnvConfigResolver:
    """Resolution from environment variables and code defaults."""

    def __init__(self, settings: LLMSettings) -> None:
        self._config = ResolvedConfig(settings)

    async def resolve(self, task: LLMTask) -> ModelAssignment:
        return self._config.assignment(task)

    async def cloud_allowed(self) -> bool:
        return self._config.cloud_enabled

    async def structured_output_retries(self) -> int:
        return self._config.settings.structured_output_retries

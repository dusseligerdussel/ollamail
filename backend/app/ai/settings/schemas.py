"""Admin API for AI settings. API keys are write-only: responses say only whether one
is set (``api_key_set``)."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.ai.llm.types import LLMTask
from app.core.config import LLMProfileName, LLMProviderKind, StructuredOutputMode

ProviderName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$", max_length=32)
]
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
ApiKey = Annotated[SecretStr, Field(min_length=1, max_length=4096)]
Timeout = Annotated[float, Field(gt=0, le=3600)]

ProviderSource = Literal["database", "environment"]
ConnectionErrorCode = Literal["unreachable", "unauthorized", "rejected", "failed"]


def check_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("must be an http(s) URL like http://ollama:11434")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("must not contain credentials, a query or a fragment")
    return value


def display_url(value: str) -> str:
    """URL without user info (environment URLs may carry credentials)."""
    parts = urlsplit(value)
    if not (parts.username or parts.password):
        return value
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return parts._replace(netloc=netloc).geturl()


class _ProviderFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: LLMProviderKind
    # Ollama: server root (http://host:11434). OpenAI-compatible: API root incl. /v1.
    base_url: Annotated[str, Field(max_length=2048)]
    timeout: Timeout | None = None

    _check_url = field_validator("base_url")(check_base_url)


class AIProviderCreate(_ProviderFields):
    name: ProviderName
    display_name: DisplayName
    api_key: ApiKey | None = None
    # Data leaves the instance; used only while cloud LLMs are allowed.
    is_cloud: bool = False
    structured_output: StructuredOutputMode = "native"


class AIProviderUpdate(BaseModel):
    """Omitted fields stay unchanged; ``api_key: null`` removes the key."""

    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    kind: LLMProviderKind | None = None
    base_url: Annotated[str, Field(max_length=2048)] | None = None
    api_key: ApiKey | None = None
    is_cloud: bool | None = None
    structured_output: StructuredOutputMode | None = None
    timeout: Timeout | None = None

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return None if value is None else check_base_url(value)


class AIProviderTest(_ProviderFields):
    """Unsaved settings to test. Without ``api_key``, the stored key of ``name`` is used
    (so an edit form can be tested without typing the key again)."""

    name: ProviderName | None = None
    api_key: ApiKey | None = None


class AIProviderRead(BaseModel):
    name: str
    display_name: str
    kind: LLMProviderKind
    base_url: str
    api_key_set: bool
    is_cloud: bool
    structured_output: StructuredOutputMode
    timeout: float | None
    # Providers from the environment (OLLAMAIL_LLM_*) are read-only.
    source: ProviderSource
    # Tasks currently served by this provider.
    used_by: list[LLMTask]


class ConnectionTestResult(BaseModel):
    ok: bool
    # Models the endpoint serves (empty if the test failed).
    models: list[str] = Field(default_factory=list)
    error: ConnectionErrorCode | None = None
    status_code: int | None = None
    duration_ms: int


class TaskAssignmentUpdate(BaseModel):
    """``provider`` and ``model`` both ``null``: back to the environment's default."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: ModelName | None = None

    @model_validator(mode="after")
    def _model_with_provider(self) -> "TaskAssignmentUpdate":
        # Another provider rarely serves the default model under the same name.
        if self.provider is not None and self.model is None:
            raise ValueError("a model is required when a provider is chosen")
        return self


class AISettingsUpdate(BaseModel):
    """Omitted fields stay unchanged; ``null`` resets a field to the environment."""

    model_config = ConfigDict(extra="forbid")

    cloud_enabled: bool | None = None
    profile: LLMProfileName | None = None
    concurrency: Annotated[int, Field(ge=1)] | None = None
    # Only the listed tasks change.
    tasks: dict[LLMTask, TaskAssignmentUpdate] | None = None


class TaskSettingRead(BaseModel):
    task: LLMTask
    # Stored choice (null: environment default).
    provider: str | None
    model: str | None
    # What serves the task now.
    effective_provider: str
    effective_model: str
    # What serves it without a stored choice.
    default_provider: str
    default_model: str
    # Assigned to a cloud provider while cloud LLMs are not allowed: the task fails.
    blocked: bool


class ProfileRead(BaseModel):
    name: LLMProfileName
    chat_model: str
    embedding_model: str
    context_tokens: int


class AISettingsRead(BaseModel):
    cloud_enabled: bool
    cloud_enabled_default: bool
    profile: LLMProfileName
    profile_default: LLMProfileName
    profiles: list[ProfileRead]
    context_tokens: int
    # Parallel LLM requests per worker process.
    concurrency: int
    concurrency_default: int
    concurrency_max: int
    tasks: list[TaskSettingRead]


class CloudUsage(BaseModel):
    provider: str
    display_name: str
    tasks: list[LLMTask]


class AIStatusRead(BaseModel):
    """For every user: which tasks send mail content to which cloud provider."""

    cloud: list[CloudUsage]

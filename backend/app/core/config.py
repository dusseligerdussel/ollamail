"""Application settings.

All configuration comes from environment variables with the prefix ``OLLAMAIL_``.
Settings are grouped by concern; each group reads its own sub-prefix, e.g.
``DatabaseSettings.url`` is read from ``OLLAMAIL_DATABASE_URL``.

New modules add their settings here additively (new group or new fields) and document
them in ``deploy/.env.example``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "OLLAMAIL_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
# Job queues, see app/worker.py.
QueueName = Literal["sync", "llm", "tts", "default"]


def _config(group: str = "") -> SettingsConfigDict:
    return SettingsConfigDict(env_prefix=f"{ENV_PREFIX}{group}", extra="ignore")


class LoggingSettings(BaseSettings):
    """``OLLAMAIL_LOG_*``"""

    model_config = _config("LOG_")

    level: LogLevel = "INFO"
    # "json" for production, "console" for human-readable local development output.
    format: Literal["json", "console"] = "json"

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value


class DatabaseSettings(BaseSettings):
    """``OLLAMAIL_DATABASE_*``"""

    model_config = _config("DATABASE_")

    url: SecretStr = SecretStr("postgresql+asyncpg://ollamail:ollamail@localhost:5432/ollamail")
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    # Seconds to wait for a new connection to be established.
    connect_timeout: float = Field(default=5.0, gt=0)

    @field_validator("url")
    @classmethod
    def _require_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must use the 'postgresql+asyncpg://' scheme")
        return value


class SecuritySettings(BaseSettings):
    """``OLLAMAIL_*`` (security)"""

    model_config = _config()

    # Master key for encrypting stored secrets (see docs/PRIVACY.md and app/core/crypto.py):
    # base64 of at least 32 random bytes. The app refuses to start without a valid key.
    secret_key: SecretStr | None = None
    # Previous master keys, comma-separated. Still accepted for decryption until
    # `python -m app.cli rotate-keys` has re-encrypted everything with ``secret_key``.
    secret_keys_old: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)
    # Token required by ``POST /api/setup`` to create the first admin. If unset, one is
    # derived from ``secret_key`` and logged at start-up while no user exists.
    setup_token: SecretStr | None = None

    @field_validator("secret_keys_old", mode="before")
    @classmethod
    def _split_keys(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


LLMProviderKind = Literal["ollama", "openai_compatible"]
LLMProfileName = Literal["cpu", "gpu-consumer", "gpu-server"]
StructuredOutputMode = Literal["native", "prompt"]


class LLMEndpointSettings(BaseModel):
    """An additional named LLM endpoint (entry of ``OLLAMAIL_LLM_ENDPOINTS``)."""

    provider: LLMProviderKind = "ollama"
    # Ollama: server root (``http://host:11434``). OpenAI-compatible: API root incl. ``/v1``.
    base_url: str
    api_key: SecretStr | None = None
    # Data leaves the instance: only used while ``cloud_enabled`` is on.
    is_cloud: bool = False
    # "prompt" for servers without JSON-schema support in the API.
    structured_output: StructuredOutputMode = "native"
    timeout: float | None = Field(default=None, gt=0)


class StorageSettings(BaseSettings):
    """``OLLAMAIL_*`` (file storage)"""

    model_config = _config()

    # Root directory for attachments, audio digests and other files.
    data_dir: Path = Path("/data")


class LLMSettings(BaseSettings):
    """``OLLAMAIL_LLM_*``

    The fields without prefix describe the ``default`` endpoint. Model selection: a task
    override (``TASK_<TASK>_MODEL``) beats a global override (``DEFAULT_*_MODEL``), which
    beats the hardware profile (``PROFILE``), see ``app/ai/llm/profiles.py``.
    """

    model_config = _config("LLM_")

    # Global admin switch: cloud LLM endpoints are opt-in (local first).
    cloud_enabled: bool = False
    # Parallel jobs on the ``llm`` queue per worker process. Keep low on CPU-only hosts.
    concurrency: int = Field(default=1, ge=1)

    provider: LLMProviderKind = "ollama"
    base_url: str = "http://ollama:11434"
    api_key: SecretStr | None = None
    is_cloud: bool = False
    structured_output: StructuredOutputMode = "native"
    # Seconds per request; CPU inference of long prompts is slow.
    timeout: float = Field(default=300.0, gt=0)
    # Further endpoints as JSON object: {"<name>": {"provider": ..., "base_url": ...}}.
    endpoints: dict[str, LLMEndpointSettings] = Field(default_factory=dict)

    profile: LLMProfileName = "cpu"
    default_chat_model: str | None = None
    default_embedding_model: str | None = None
    context_tokens: int | None = Field(default=None, ge=512)

    # Per-task model/endpoint (endpoint: "default" or a key of ``endpoints``).
    task_triage_model: str | None = None
    task_triage_endpoint: str | None = None
    task_todos_model: str | None = None
    task_todos_endpoint: str | None = None
    task_digest_model: str | None = None
    task_digest_endpoint: str | None = None
    task_rag_chat_model: str | None = None
    task_rag_chat_endpoint: str | None = None
    task_embeddings_model: str | None = None
    task_embeddings_endpoint: str | None = None

    # Extra attempts when a model returns output that does not match the schema.
    structured_output_retries: int = Field(default=2, ge=0, le=5)
    # Add an "llm" check to /readyz (all assigned models available).
    readiness_check: bool = False
    # Pull missing models from Ollama endpoints in the background on start.
    pull_missing_models: bool = False

    @model_validator(mode="after")
    def _known_endpoints(self) -> "LLMSettings":
        if "default" in self.endpoints:
            raise ValueError("endpoint name 'default' is reserved")
        known = {"default", *self.endpoints}
        for name, value in self:
            is_task_endpoint = name.startswith("task_") and name.endswith("_endpoint")
            if is_task_endpoint and value is not None and value not in known:
                raise ValueError(f"{name}: unknown endpoint {value!r}")
        return self


class MailSettings(BaseSettings):
    """``OLLAMAIL_MAIL_*``"""

    model_config = _config("MAIL_")

    # Default time window for the initial import of a mailbox (data minimisation).
    initial_sync_days: int = Field(default=90, ge=1)
    # Admin flag: allow unencrypted IMAP connections and unverified TLS certificates.
    allow_insecure_connections: bool = False
    # Seconds to wait for a mail server response before the connection is dropped.
    imap_timeout: float = Field(default=60.0, gt=0)
    # Messages fetched (and committed) per batch; an interrupted sync resumes per batch.
    sync_batch_size: int = Field(default=50, ge=1, le=1000)
    # Keep one push connection (IMAP IDLE) per mailbox in the worker; otherwise poll only.
    watch_enabled: bool = True


class TTSSettings(BaseSettings):
    """``OLLAMAIL_TTS_*``"""

    model_config = _config("TTS_")

    engine: str = "piper"

    # Default voice per language (engine-specific ID). Users may pick another installed
    # voice of the same language; see app/ai/tts/voices.py.
    voice_de: str = "de_DE-thorsten-medium"
    voice_en: str = "en_US-ljspeech-medium"
    # Download missing voices into <data_dir>/tts/voices/<engine>/. Disable on hosts
    # without internet access and copy the voice files there manually.
    download_voices: bool = True
    voice_base_url: str = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
    # Seconds per voice download (models are 20-120 MB).
    download_timeout: float = Field(default=600.0, gt=0)

    # Encoding (ffmpeg): Opus is the default format, MP3 for podcast apps.
    ffmpeg_path: str = "ffmpeg"
    opus_bitrate_kbps: int = Field(default=32, ge=6, le=256)
    mp3_bitrate_kbps: int = Field(default=64, ge=32, le=320)

    # Long texts are synthesised piece by piece; a piece holds at most this many characters.
    max_chunk_chars: int = Field(default=400, ge=50, le=5000)
    # Silence in seconds after a sentence and after a paragraph.
    sentence_pause: float = Field(default=0.35, ge=0, le=5)
    paragraph_pause: float = Field(default=0.8, ge=0, le=5)
    # Speaking rate: values above 1 speak slower (Piper ``length_scale``); unset = voice default.
    length_scale: float | None = Field(default=None, gt=0.25, le=4)


class AuthSettings(BaseSettings):
    """``OLLAMAIL_AUTH_*``"""

    model_config = _config("AUTH_")

    # Absolute session lifetime and idle timeout (no request in that time) in minutes.
    session_lifetime_minutes: int = Field(default=14 * 24 * 60, ge=5)
    session_idle_timeout_minutes: int = Field(default=3 * 24 * 60, ge=5)
    # Send cookies only over HTTPS. Disable only for plain-HTTP setups without TLS.
    cookie_secure: bool = True
    # Self-service registration of local accounts (role "user"). Admins can always add users.
    local_registration: bool = False
    password_min_length: int = Field(default=12, ge=8, le=128)
    # Lockout: after this many login attempts for one account within the window, further
    # attempts are rejected until the window ends (counted per account, existing or not).
    login_max_attempts: int = Field(default=5, ge=1)
    login_window_minutes: int = Field(default=15, ge=1)
    # Login/registration attempts per client IP and window. Behind a reverse proxy the client
    # IP comes from X-Forwarded-For (uvicorn --forwarded-allow-ips).
    ip_max_attempts: int = Field(default=50, ge=1)
    # Allow LDAP directories without TLS (tls_mode "none"). Passwords then travel in clear
    # text; only for test setups or networks that are encrypted otherwise.
    ldap_allow_plaintext: bool = False

    @model_validator(mode="after")
    def _idle_within_lifetime(self) -> "AuthSettings":
        if self.session_idle_timeout_minutes > self.session_lifetime_minutes:
            raise ValueError("session_idle_timeout_minutes exceeds session_lifetime_minutes")
        return self


class ProcessingSettings(BaseSettings):
    """``OLLAMAIL_PROCESSING_*`` (mail processing pipeline, app/processing/)"""

    model_config = _config("PROCESSING_")

    # Global switch: off = new mails are stored but not triaged, indexed etc.
    enabled: bool = True
    # Messages queued per run of the periodic job that re-processes outdated messages.
    requeue_batch_size: int = Field(default=500, ge=1)


class TriageSettings(BaseSettings):
    """``OLLAMAIL_TRIAGE_*`` (mail triage, app/triage/)"""

    model_config = _config("TRIAGE_")

    # Rule-based pre-filter (List-Unsubscribe, Precedence, Auto-Submitted, sender rules)
    # before the LLM; saves inference time on CPU-only hosts.
    prefilter_enabled: bool = True
    # Characters of the mail body sent to the model (the rest is cut off).
    max_body_chars: int = Field(default=2000, ge=200, le=50000)
    # Few-shot examples from the user's own corrections per classification (0 = none).
    few_shot_examples: int = Field(default=4, ge=0, le=20)
    # Characters of each example mail's body in the prompt.
    few_shot_body_chars: int = Field(default=400, ge=50, le=5000)
    # Most recent corrections considered when picking the most similar examples.
    few_shot_pool: int = Field(default=200, ge=1, le=2000)
    # Pick the most similar examples via embeddings (if an embedding model is available);
    # otherwise the most recent corrections are used.
    few_shot_embeddings: bool = True
    # Corrections of one sender to the same category before a sender rule is suggested.
    rule_suggestion_min_corrections: int = Field(default=3, ge=1)
    # Prefix of the keyword/label/folder written back to the server, e.g. "ollamail/info".
    label_prefix: str = Field(default="ollamail/", pattern=r"^[A-Za-z0-9_./-]{0,32}$")


class SearchSettings(BaseSettings):
    """``OLLAMAIL_SEARCH_*`` (hybrid search index, app/search/)"""

    model_config = _config("SEARCH_")

    # Length of the vectors of the embedding model (bge-m3: 1024). Read by the migration
    # that creates the index; changing it later needs ``python -m app.cli search resize``
    # (docs/OPERATIONS.md). pgvector's HNSW index supports at most 2000 dimensions.
    embedding_dimensions: int = Field(default=1024, ge=1, le=2000)

    # Chunking: target size and overlap of neighbouring chunks, in characters.
    chunk_size: int = Field(default=1200, ge=200, le=8000)
    chunk_overlap: int = Field(default=200, ge=0, le=2000)
    # Upper bound per message (body and attachments together); the rest is not indexed.
    max_chunks_per_message: int = Field(default=200, ge=1)

    # Texts per embedding request and pause between requests (seconds): keeps CPU-only
    # hosts responsive while a large mailbox is indexed.
    embed_batch_size: int = Field(default=16, ge=1, le=512)
    embed_pause_seconds: float = Field(default=0.0, ge=0, le=60)
    # Chunks per run of the background job that fills in missing embeddings
    # (model switch, LLM unavailable while indexing).
    reembed_batch_size: int = Field(default=256, ge=1)

    # Attachment text extraction (PDF, DOCX, TXT, HTML) in a separate process.
    attachment_max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    attachment_max_chars: int = Field(default=200_000, ge=1000)
    extraction_timeout: float = Field(default=30.0, gt=0, le=600)
    extraction_max_memory_mb: int = Field(default=1024, ge=128)

    # Retrieval: candidates per index (full text, vectors) before Reciprocal Rank Fusion,
    # and the RRF constant k (score = sum of 1 / (k + rank)).
    candidates: int = Field(default=50, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _overlap_below_size(self) -> "SearchSettings":
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError("chunk_overlap must be less than half of chunk_size")
        return self


class TodosSettings(BaseSettings):
    """``OLLAMAIL_TODOS_*`` (todo extraction, app/todos/)"""

    model_config = _config("TODOS_")

    # Pipeline step on/off; the API for manual todos stays available.
    extraction_enabled: bool = True
    # Triage categories (keys, case-insensitive) whose mails are not searched for todos,
    # comma-separated in the environment. Mails without a triage result are processed.
    skip_categories: Annotated[list[str], NoDecode] = Field(
        default=["newsletter", "notification", "spam"]
    )
    # Extracted todos below this model confidence (0-1) are discarded.
    min_confidence: float = Field(default=0.5, ge=0, le=1)

    @field_validator("skip_categories", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value


class WorkerSettings(BaseSettings):
    """``OLLAMAIL_WORKER_*``"""

    model_config = _config("WORKER_")

    # Queues this worker process consumes, comma-separated in the environment
    # (e.g. ``OLLAMAIL_WORKER_QUEUES=llm`` for a dedicated LLM worker).
    queues: Annotated[list[QueueName], NoDecode] = Field(
        default=["sync", "llm", "tts", "default"], min_length=1
    )
    # Parallel jobs for all consumed queues except ``llm`` (see ``LLMSettings.concurrency``).
    concurrency: int = Field(default=4, ge=1)
    # Seconds running jobs get to finish after SIGTERM before they are cancelled.
    shutdown_timeout: float = Field(default=30.0, ge=0)

    @field_validator("queues", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


class AuditSettings(BaseSettings):
    """``OLLAMAIL_AUDIT_*`` (audit log, app/audit/)"""

    model_config = _config("AUDIT_")

    # Days audit events are kept; 0 keeps them forever. Enforced by the retention job (#36).
    retention_days: int = Field(default=365, ge=0)


class Settings(BaseModel):
    """All settings, grouped by concern."""

    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)

    todos: TodosSettings = Field(default_factory=TodosSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()

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
QueueName = Literal["sync", "llm", "tts", "ocr", "default"]


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
    # Parallel LLM requests per worker process. Keep low on CPU-only hosts. Default for
    # the admin setting (AI page), which can change it at runtime up to ``max_concurrency``.
    concurrency: int = Field(default=1, ge=1)
    # Job slots of the ``llm`` queue per worker process: upper bound for ``concurrency``.
    max_concurrency: int = Field(default=4, ge=1)

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
    task_reply_draft_model: str | None = None
    task_reply_draft_endpoint: str | None = None
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


class GraphSettings(BaseSettings):
    """``OLLAMAIL_MAIL_GRAPH_*`` (Microsoft 365 via Graph, docs/providers/microsoft365.md)"""

    model_config = _config("MAIL_GRAPH_")

    # Entra ID app registration. Without a client ID, Microsoft 365 mailboxes are disabled.
    client_id: str | None = None
    client_secret: SecretStr | None = None
    # Tenant for sign-in and app-only tokens: a tenant ID, or "organizations" (any work
    # account, delegated only). App-only mailboxes need a real tenant ID.
    tenant_id: str = "organizations"
    # Redirect URI registered in Entra ID; unset: <scheme>://<host>/api/mail/graph/callback.
    redirect_uri: str | None = None
    # Request ``Mail.Send`` when a mailbox is connected, so replies can be sent (app/drafts).
    # Off: mailboxes connect with read/write access only and sending is refused.
    send_enabled: bool = True
    # Public URL of /api/mail/graph/notifications for change notifications (webhooks).
    # Unset (default): polling only, no inbound connections from Microsoft.
    notification_url: str | None = None
    # Endpoints; change only for national clouds or tests.
    authority: str = "https://login.microsoftonline.com"
    api_url: str = "https://graph.microsoft.com/v1.0"
    # Seconds per request and retries after throttling (429/503, honouring Retry-After).
    timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=5, ge=0, le=20)

    @field_validator("redirect_uri", "notification_url", "authority", "api_url", mode="before")
    @classmethod
    def _url(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("must be an http(s) URL")
        return value

    @property
    def enabled(self) -> bool:
        return bool(self.client_id)


class GmailSettings(BaseSettings):
    """``OLLAMAIL_GMAIL_*`` (Gmail / Google Workspace provider, docs/providers/gmail.md)"""

    model_config = _config("GMAIL_")

    # OAuth client (type "web application") for connecting mailboxes per user.
    client_id: str | None = None
    client_secret: SecretStr | None = None
    # Callback URL exactly as registered at Google, e.g.
    # http://localhost:8080/api/mail/gmail/oauth/callback (needs no public reachability).
    redirect_uri: str | None = None
    # Request ``gmail.readonly`` instead of ``gmail.modify``; actions are refused.
    readonly: bool = False
    # Service account key (JSON) for Workspace domain-wide delegation and Pub/Sub pull.
    service_account_file: Path | None = None
    # Seconds per Gmail API request.
    timeout: float = Field(default=60.0, gt=0)


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


OIDCPresetName = Literal["generic", "entra", "google", "keycloak", "authentik"]


class OIDCProviderSettings(BaseModel):
    """An OIDC provider configured via ``OLLAMAIL_AUTH_OIDC_PROVIDERS`` (GitOps).

    Same fields as the admin API (``app/auth/providers/oidc``); see docs/auth/oidc.md.
    """

    display_name: str = Field(min_length=1, max_length=255)
    preset: OIDCPresetName = "generic"
    issuer: str
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: SecretStr | None = None
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])
    enabled: bool = True
    # Just-in-time provisioning: create unknown users on their first login.
    auto_provision: bool = True
    # Link to an existing user with the same e-mail address (verified e-mail only).
    link_by_email: bool = False
    # E-mail domains allowed to sign in (empty: all).
    allowed_domains: list[str] = Field(default_factory=list)
    # Claim with group names/IDs, stored for the role mapping (empty: none).
    groups_claim: str | None = "groups"
    # Entra ID: allowed tenant IDs (``tid``), required for multi-tenant issuers.
    allowed_tenants: list[str] = Field(default_factory=list)
    # Google Workspace: allowed hosted domains (``hd``).
    hosted_domains: list[str] = Field(default_factory=list)


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
    # Public URL of the web UI (e.g. https://mail.example.org), used for OIDC redirect URIs.
    # Unset: derived from the request (Host / X-Forwarded-Proto).
    public_url: str | None = None
    # OIDC providers from the environment (JSON object {"<name>": {...}}); read-only in the
    # admin API. More can be added in the admin API (stored in the database).
    oidc_providers: dict[str, OIDCProviderSettings] = Field(default_factory=dict)
    # Allow http:// issuers (local test IdPs only; TLS is mandatory otherwise).
    oidc_allow_insecure_http: bool = False
    # Seconds discovery documents and signing keys (JWKS) are cached.
    oidc_metadata_cache_seconds: int = Field(default=3600, ge=0)
    # Validity of invitation links for local accounts (admin user list), in hours.
    invitation_lifetime_hours: int = Field(default=7 * 24, ge=1, le=90 * 24)
    # Second factor for local accounts (app/auth/mfa): minutes between the password and the
    # second factor (or enrolling one) before the login has to start over.
    mfa_pending_minutes: int = Field(default=5, ge=1, le=30)
    # Passkeys (WebAuthn): relying party ID (the domain, e.g. mail.example.org) and the
    # origins the browser may report (JSON list, e.g. ["https://mail.example.org"]).
    # Unset: both derived from public_url. Without either, passkeys are unavailable.
    webauthn_rp_id: str | None = None
    webauthn_origins: list[str] = Field(default_factory=list)

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        value = value.strip().rstrip("/")
        scheme, _, rest = value.partition("://")
        if scheme not in {"https", "http"} or not rest or any(c in rest for c in "?#@"):
            raise ValueError("must be an http(s) URL like https://mail.example.org")
        return value

    @field_validator("webauthn_rp_id")
    @classmethod
    def _check_rp_id(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None
        value = value.strip().lower()
        if any(c in value for c in "/:?#@ "):
            raise ValueError("must be a domain like mail.example.org")
        return value

    @field_validator("webauthn_origins")
    @classmethod
    def _check_origins(cls, value: list[str]) -> list[str]:
        origins = [origin.strip().rstrip("/") for origin in value if origin.strip()]
        for origin in origins:
            scheme, _, rest = origin.partition("://")
            if scheme not in {"https", "http"} or not rest or any(c in rest for c in "/?#@"):
                raise ValueError("origins must look like https://mail.example.org")
        return origins

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

    # OCR of scanned attachments with Tesseract (docs/OPERATIONS.md, OCR): ``off``,
    # ``pdf`` (PDF pages without text) or ``all`` (also PNG, JPEG and TIFF images).
    # Runs as job on the ``ocr`` queue, in the same isolated process as the extraction.
    ocr_mode: Literal["off", "pdf", "all"] = "pdf"
    # Pages recognised per PDF at most (pages with a text layer do not count).
    ocr_max_pages: int = Field(default=20, ge=1, le=1000)
    # Tesseract language packs, ``+``-separated (the image contains ``deu`` and ``eng``).
    ocr_languages: str = Field(default="deu+eng", pattern=r"^[a-z_]{3,16}(\+[a-z_]{3,16}){0,7}$")
    # Run time limit of one attachment's OCR, in seconds.
    ocr_timeout: float = Field(default=300.0, gt=0, le=3600)
    # Parallel OCR jobs per worker process (``ocr`` queue); each uses one CPU core.
    ocr_concurrency: int = Field(default=1, ge=1, le=64)

    # Retrieval: candidates per index (full text, vectors) before Reciprocal Rank Fusion,
    # and the RRF constant k (score = sum of 1 / (k + rank)).
    candidates: int = Field(default=50, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _overlap_below_size(self) -> "SearchSettings":
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError("chunk_overlap must be less than half of chunk_size")
        return self


class RagSettings(BaseSettings):
    """``OLLAMAIL_RAG_*`` ("ask your inbox", app/rag/)"""

    model_config = _config("RAG_")

    # Extract filters (period, sender, mailbox, category) and a standalone search query
    # from the question with the LLM. Off: the question is searched as typed, only the
    # filters set in the UI apply.
    filter_extraction_enabled: bool = True
    # Chunks retrieved per question; the best ones that fit the context window are passed
    # to the model as numbered sources.
    retrieval_limit: int = Field(default=12, ge=1, le=100)
    # Rerank the retrieved chunks with the chat model before answering. ``None`` (default)
    # follows the hardware profile: on for GPU profiles, off for ``cpu``.
    reranker_enabled: bool | None = None
    # Chunks handed to the reranker (it picks ``retrieval_limit`` of them).
    rerank_candidates: int = Field(default=24, ge=2, le=100)
    # Upper bound for the answer, in tokens.
    max_answer_tokens: int = Field(default=1024, ge=64, le=8192)
    # Earlier questions and answers of the conversation passed to the model (follow-ups).
    history_turns: int = Field(default=3, ge=0, le=20)
    # Characters of a mail chunk stored and shown as excerpt of a citation.
    snippet_chars: int = Field(default=400, ge=50, le=4000)
    # Days a conversation is kept after its last question; 0 keeps conversations until the
    # user deletes them. Enforced by the daily job ``rag.purge_conversations``.
    history_retention_days: int = Field(default=90, ge=0)


class DraftsSettings(BaseSettings):
    """``OLLAMAIL_DRAFTS_*`` (reply drafts and sending, app/drafts/)"""

    model_config = _config("DRAFTS_")

    # Mails of the thread (up to and including the one answered) given to the model.
    thread_messages: int = Field(default=6, ge=1, le=30)
    # Characters per mail in the prompt (the beginning is kept, quotes are already removed).
    message_chars: int = Field(default=3000, ge=200, le=50000)
    # Upper bound for a generated draft, in tokens.
    max_tokens: int = Field(default=800, ge=64, le=8192)
    # Own sent mails passed as style examples if the user has not switched them off;
    # 0 disables style examples for everybody.
    style_examples: int = Field(default=3, ge=0, le=10)
    # Characters per style example.
    style_example_chars: int = Field(default=800, ge=100, le=5000)
    # Days a draft (sent, discarded or unsent) is kept after its last change; 0 keeps
    # drafts until the user deletes them. Enforced by the daily job ``drafts.purge``.
    retention_days: int = Field(default=30, ge=0, le=3650)


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
    # Export targets users may connect (app/todos/export/), comma-separated, e.g. ``caldav``.
    # Empty (default): no export. The export sends todo titles and descriptions to a server
    # the user names, so the admin opts in (docs/PRIVACY.md).
    export_sinks: Annotated[list[Literal["caldav"]], NoDecode] = Field(default=[])
    # Allow http:// CalDAV servers. Credentials then travel in clear text; only for test
    # setups or networks that are encrypted otherwise.
    export_allow_http: bool = False
    # Minutes between two status checks of exported todos ("done" set in the target system).
    export_poll_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # Seconds per request to an export target.
    export_timeout_seconds: float = Field(default=20.0, gt=0, le=300)

    @field_validator("skip_categories", "export_sinks", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip().lower() for part in value.split(",") if part.strip()]
        return value


class DigestSettings(BaseSettings):
    """``OLLAMAIL_DIGEST_*`` (daily digest and podcast feed, app/digest/)"""

    model_config = _config("DIGEST_")

    # Scheduler on/off; manual digests stay available.
    enabled: bool = True
    # Days a digest (script and audio files) is kept before it is deleted automatically.
    retention_days: int = Field(default=30, ge=1, le=3650)
    # Period of a user's first digest (there is no previous one to continue from).
    first_lookback_hours: int = Field(default=24, ge=1, le=24 * 31)
    # Upper bound of a digest's period, e.g. after a long pause.
    max_lookback_days: int = Field(default=7, ge=1, le=31)
    # Mails summarised per digest (most important first); the rest is only counted.
    max_messages: int = Field(default=60, ge=1, le=1000)
    # Mails per map call; small models stay reliable with few items per answer.
    map_batch_size: int = Field(default=6, ge=1, le=50)
    # Characters of a mail body given to the model in the map step.
    map_body_chars: int = Field(default=1500, ge=200, le=20000)
    # Triage categories (keys) mentioned only as one collective sentence, and skipped ones.
    bulk_categories: Annotated[list[str], NoDecode] = Field(default=["newsletter", "notification"])
    skip_categories: Annotated[list[str], NoDecode] = Field(default=["spam"])
    # Speak the script (TTS); without audio digests are text only and not in the feed.
    audio_enabled: bool = True
    # Audio formats per digest: MP3 for podcast apps, Opus (smaller) for the web player.
    audio_formats: Annotated[list[Literal["mp3", "opus"]], NoDecode] = Field(
        default=["mp3", "opus"], min_length=1
    )

    @field_validator("bulk_categories", "skip_categories", "audio_formats", mode="before")
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
        default=["sync", "llm", "tts", "ocr", "default"], min_length=1
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

    # Days audit events are kept; 0 keeps them forever. Enforced by ``privacy.retention``.
    retention_days: int = Field(default=365, ge=0)


class PrivacySettings(BaseSettings):
    """``OLLAMAIL_PRIVACY_*`` (retention, data export and account deletion, app/privacy/)"""

    model_config = _config("PRIVACY_")

    # Default retention in days, by age of the mail (received, else sent, else imported);
    # 0 keeps the data. Admins override them (and the digest, conversation and audit
    # retention) in the admin area; the daily job ``privacy.retention`` enforces them.
    # Mails, with attachments, search index, triage results and citations.
    mail_retention_days: int = Field(default=0, ge=0, le=36500)
    # Attachment files only (the mail stays).
    attachment_retention_days: int = Field(default=0, ge=0, le=36500)
    # Search index (text chunks and embeddings) only; the mail stays but is no longer
    # found by search or "ask your inbox".
    search_index_retention_days: int = Field(default=0, ge=0, le=36500)
    # Hours a finished data export can be downloaded before it is deleted.
    export_expiry_hours: int = Field(default=24, ge=1, le=24 * 30)
    # Users may delete their own account (Art. 17). Off: only admins delete users.
    self_delete_enabled: bool = True


class ScimSettings(BaseSettings):
    """``OLLAMAIL_SCIM_*`` (SCIM 2.0 provisioning, app/scim/)"""

    model_config = _config("SCIM_")

    # Requests per SCIM token and minute; more get 429 with Retry-After.
    rate_limit_per_minute: int = Field(default=600, ge=1)
    # Requests with a missing or wrong token per client IP within 15 minutes.
    failed_auth_per_ip: int = Field(default=20, ge=1)
    # Largest page of a list request (``count``); also the default page size.
    max_results: int = Field(default=200, ge=1, le=1000)


class Settings(BaseModel):
    """All settings, grouped by concern."""

    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    rag: RagSettings = Field(default_factory=RagSettings)
    drafts: DraftsSettings = Field(default_factory=DraftsSettings)

    todos: TodosSettings = Field(default_factory=TodosSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    scim: ScimSettings = Field(default_factory=ScimSettings)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()

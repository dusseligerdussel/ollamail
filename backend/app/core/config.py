"""Application settings.

All configuration comes from environment variables with the prefix ``OLLAMAIL_``.
Settings are grouped by concern; each group reads its own sub-prefix, e.g.
``DatabaseSettings.url`` is read from ``OLLAMAIL_DATABASE_URL``.

New modules add their settings here additively (new group or new fields) and document
them in ``deploy/.env.example``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "OLLAMAIL_"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


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

    # Master key for encrypting stored secrets (see docs/PRIVACY.md). Required once
    # secret storage is implemented.
    secret_key: SecretStr | None = None


class StorageSettings(BaseSettings):
    """``OLLAMAIL_*`` (file storage)"""

    model_config = _config()

    # Root directory for attachments, audio digests and other files.
    data_dir: Path = Path("/data")


class LLMSettings(BaseSettings):
    """``OLLAMAIL_LLM_*``"""

    model_config = _config("LLM_")

    # Global admin switch: cloud LLM endpoints are opt-in (local first).
    cloud_enabled: bool = False


class MailSettings(BaseSettings):
    """``OLLAMAIL_MAIL_*``"""

    model_config = _config("MAIL_")

    # Default time window for the initial import of a mailbox (data minimisation).
    initial_sync_days: int = Field(default=90, ge=1)


class TTSSettings(BaseSettings):
    """``OLLAMAIL_TTS_*``"""

    model_config = _config("TTS_")

    engine: str = "piper"


class Settings(BaseModel):
    """All settings, grouped by concern."""

    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read once from the environment."""
    return Settings()

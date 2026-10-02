"""AI providers and instance-wide AI settings. API keys are encrypted at rest."""

from typing import Any

from sqlalchemy import CheckConstraint, Float, String, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedStr
from app.core.db import Base


class AIProviderRecord(Base):
    """An LLM endpoint added in the admin API (next to those from the environment)."""

    __tablename__ = "ai_providers"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # ``LLMProviderKind``: "ollama" or "openai_compatible".
    kind: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(2048))
    api_key: Mapped[str | None] = mapped_column(EncryptedStr)
    # Data leaves the instance: only used while cloud LLMs are allowed.
    is_cloud: Mapped[bool] = mapped_column(default=False)
    # ``StructuredOutputMode``: "native" or "prompt".
    structured_output: Mapped[str] = mapped_column(String(16), default="native")
    timeout: Mapped[float | None] = mapped_column(Float)


class AISettingsRecord(Base):
    """The single row of instance-wide AI settings. ``NULL`` means: use the environment."""

    __tablename__ = "ai_settings"
    __table_args__ = (CheckConstraint("singleton", name="singleton"),)

    singleton: Mapped[bool] = mapped_column(server_default=true(), default=True, unique=True)
    cloud_enabled: Mapped[bool | None] = mapped_column()
    # ``LLMProfileName``.
    profile: Mapped[str | None] = mapped_column(String(32))
    concurrency: Mapped[int | None] = mapped_column()
    # {"<task>": {"provider": "<name>" | null, "model": "<model>" | null}}
    tasks: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

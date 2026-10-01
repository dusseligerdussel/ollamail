import pytest
from pydantic import ValidationError

from app.core.config import DatabaseSettings, Settings


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/x")
    monkeypatch.setenv("OLLAMAIL_DATABASE_POOL_SIZE", "7")
    monkeypatch.setenv("OLLAMAIL_LOG_LEVEL", "debug")
    monkeypatch.setenv("OLLAMAIL_LOG_FORMAT", "console")
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", "k")
    monkeypatch.setenv("OLLAMAIL_LLM_CLOUD_ENABLED", "true")
    monkeypatch.setenv("OLLAMAIL_MAIL_INITIAL_SYNC_DAYS", "30")
    monkeypatch.setenv("OLLAMAIL_TTS_ENGINE", "kokoro")

    settings = Settings()

    assert settings.database.url.get_secret_value() == "postgresql+asyncpg://u:p@db:5432/x"
    assert settings.database.pool_size == 7
    assert settings.logging.level == "DEBUG"
    assert settings.logging.format == "console"
    assert settings.security.secret_key is not None
    assert settings.security.secret_key.get_secret_value() == "k"
    assert settings.llm.cloud_enabled is True
    assert settings.mail.initial_sync_days == 30
    assert settings.tts.engine == "kokoro"


def test_defaults_are_local_first() -> None:
    settings = Settings()

    assert settings.llm.cloud_enabled is False
    assert settings.logging.format == "json"


def test_database_url_requires_asyncpg() -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings.model_validate({"url": "postgresql://u:p@db/x"})


def test_database_url_is_not_rendered() -> None:
    settings = DatabaseSettings.model_validate({"url": "postgresql+asyncpg://u:hunter2@db/x"})

    assert "hunter2" not in repr(settings)

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from app.core.config import DatabaseSettings, MailSettings, SecuritySettings, Settings


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


def test_empty_values_from_env_example_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_SETUP_TOKEN", "")
    monkeypatch.setenv("OLLAMAIL_MAIL_GRAPH_CLIENT_ID", "")
    monkeypatch.setenv("OLLAMAIL_MAIL_GRAPH_CLIENT_SECRET", " ")

    settings = Settings()

    assert settings.security.setup_token is None
    assert settings.graph.client_id is None
    assert settings.graph.client_secret is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://ollamail:change-me@postgres:5432/ollamail",
        "postgresql+asyncpg://ollamail:change%2Dme@postgres/ollamail",
    ],
)
def test_database_url_with_placeholder_password_is_refused(url: str) -> None:
    with pytest.raises(ValidationError) as error:
        DatabaseSettings.model_validate({"url": url})

    assert "placeholder password" in str(error.value)
    assert "postgres:5432" not in str(error.value)


def test_database_url_validation_errors_hide_the_url() -> None:
    with pytest.raises(ValidationError) as error:
        DatabaseSettings.model_validate({"url": "postgresql://u:hunter2@db/x"})

    assert "hunter2" not in str(error.value)


def test_configured_setup_token_needs_32_characters() -> None:
    with pytest.raises(ValidationError) as error:
        SecuritySettings.model_validate({"setup_token": "short-but-secret"})
    assert "short-but-secret" not in str(error.value)

    token = "0123456789abcdef0123456789abcdef"
    settings = SecuritySettings.model_validate({"setup_token": token})
    assert settings.setup_token is not None
    assert settings.setup_token.get_secret_value() == token


def test_allowed_internal_hosts_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS", " IMAP.lan. , 192.168.10.0/24,fd00::1 ,"
    )

    assert MailSettings().allowed_internal_hosts == ["imap.lan", "192.168.10.0/24", "fd00::1"]


@pytest.mark.parametrize("entry", ["10.0.0.0/33", "imap lan", "imap.lan/24"])
def test_invalid_allowed_internal_hosts_are_rejected(entry: str) -> None:
    with pytest.raises(ValidationError):
        MailSettings(allowed_internal_hosts=[entry])


ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / ".env.example"


def _settings_env_names() -> set[str]:
    names = set()
    for group in Settings.model_fields.values():
        model = group.annotation
        assert isinstance(model, type) and issubclass(model, BaseSettings)
        prefix = model.model_config.get("env_prefix", "")
        names.update(f"{prefix}{field}".upper() for field in model.model_fields)
    return names


def _documented_env_names() -> set[str]:
    # Active (`NAME=value`) and commented-out (`# NAME=value`) entries both count.
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.MULTILINE))


def test_every_setting_is_documented_in_env_example() -> None:
    missing = _settings_env_names() - _documented_env_names()
    assert not missing, f"document these settings in deploy/.env.example: {sorted(missing)}"


def test_env_example_has_no_unknown_settings() -> None:
    # Compose-only variables (images, ports) are read by deploy/compose.yaml, not the app.
    compose_only = {
        "OLLAMAIL_VERSION",
        "OLLAMAIL_API_IMAGE",
        "OLLAMAIL_FRONTEND_IMAGE",
        "OLLAMAIL_HTTP_BIND",
        "OLLAMAIL_HTTP_PORT",
    }
    documented = {n for n in _documented_env_names() if n.startswith("OLLAMAIL_")}
    unknown = documented - _settings_env_names() - compose_only
    assert not unknown, f"not read by the app (typo or removed setting?): {sorted(unknown)}"

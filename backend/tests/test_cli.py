import pytest
from typer.testing import CliRunner

from app.cli import cli
from app.core.config import get_settings
from app.core.crypto import decode_key, set_keyring

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    get_settings.cache_clear()
    set_keyring(None)


def test_generate_key_prints_a_valid_key() -> None:
    result = runner.invoke(cli, ["generate-key"])

    assert result.exit_code == 0
    assert len(decode_key(result.stdout.strip())) == 32


def test_rotate_keys_fails_without_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMAIL_SECRET_KEY", raising=False)

    result = runner.invoke(cli, ["rotate-keys"])

    assert result.exit_code == 1
    assert "OLLAMAIL_SECRET_KEY is not set" in result.stderr


def test_help_lists_commands() -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "rotate-keys" in result.stdout
    assert "generate-key" in result.stdout

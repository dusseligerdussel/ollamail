"""Fixtures for second-factor tests: WebAuthn relying party ``test`` (the test client's
host), helpers to enrol factors and to sign in step by step."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pyotp
import pytest
from httpx import AsyncClient, Response

from app.core.config import AuthSettings, DatabaseSettings, SecuritySettings, Settings
from app.core.crypto import KeyRing, generate_key, set_keyring
from tests.auth.conftest import PASSWORD, login
from tests.auth.mfa.authenticator import SoftAuthenticator
from tests.conftest import TEST_DATABASE_URL

ORIGIN = "https://test"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database=DatabaseSettings.model_validate({"url": TEST_DATABASE_URL}),
        security=SecuritySettings.model_validate({"secret_key": generate_key()}),
        auth=AuthSettings.model_validate({"public_url": ORIGIN}),
    )


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    """The TOTP secret is an ``EncryptedStr``."""
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)


def code_at(secret: str, offset_steps: int = 0) -> str:
    """The TOTP code ``offset_steps`` time steps from now."""
    when = datetime.now(UTC) + timedelta(seconds=30 * offset_steps)
    return pyotp.TOTP(secret).at(when)


async def enable_totp(client: AsyncClient) -> tuple[str, list[str]]:
    """Set up TOTP for the signed-in user; returns the secret and the recovery codes."""
    setup = await client.post("/auth/mfa/totp/setup")
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    # The previous step: the current one stays unused for the login under test.
    confirm = await client.post("/auth/mfa/totp/confirm", json={"code": code_at(secret, -1)})
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["recovery_codes"]


async def add_passkey(
    client: AsyncClient, name: str = "Laptop"
) -> tuple[SoftAuthenticator, dict[str, Any]]:
    options = await client.post("/auth/mfa/passkeys/options")
    assert options.status_code == 200, options.text
    authenticator = SoftAuthenticator(origin=ORIGIN)
    response = await client.post(
        "/auth/mfa/passkeys",
        json={"name": name, "credential": authenticator.create(options.json())},
    )
    assert response.status_code == 201, response.text
    return authenticator, response.json()


async def password_step(client: AsyncClient, email: str, password: str = PASSWORD) -> Response:
    response = await login(client, email, password)
    assert response.status_code == 202, response.text
    return response

"""Fixtures for OIDC tests: a mock IdP wired into the app, providers and a login helper."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.oidc.models import OIDCProviderRecord
from app.auth.providers.oidc.presets import OIDCPreset
from app.auth.providers.oidc.store import OIDCProviderStore
from app.core.config import AuthSettings, Settings
from app.core.crypto import KeyRing, set_keyring
from app.core.db import get_db
from app.main import create_app
from tests.auth.oidc.mock_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, MockIdP
from tests.conftest import api_client

PUBLIC_URL = "https://test"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"auth": AuthSettings(public_url=PUBLIC_URL)})


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    """Client secrets are encrypted with the process-wide key ring (set up at start-up)."""
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)


@pytest.fixture
def idp() -> MockIdP:
    return MockIdP()


@dataclass
class OIDCTestApp:
    app: FastAPI
    client: AsyncClient
    idp: MockIdP

    @property
    def store(self) -> OIDCProviderStore:
        store: OIDCProviderStore = self.app.state.oidc
        return store

    def use_idp(self, idp: MockIdP) -> None:
        self.idp = idp
        self.store.transport = lambda: httpx.ASGITransport(app=idp.app())
        self.store.metadata.clear()


async def make_app(settings: Settings, db_session: AsyncSession, idp: MockIdP) -> OIDCTestApp:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    test_app = OIDCTestApp(app=app, client=api_client(app), idp=idp)
    test_app.use_idp(idp)
    return test_app


@pytest.fixture
async def oidc(
    settings: Settings, db_session: AsyncSession, idp: MockIdP
) -> AsyncIterator[OIDCTestApp]:
    test_app = await make_app(settings, db_session, idp)
    async with test_app.client:
        yield test_app
    await test_app.app.state.database.dispose()


async def add_provider(
    db: AsyncSession, name: str = "test", **overrides: Any
) -> OIDCProviderRecord:
    values: dict[str, Any] = {
        "name": name,
        "display_name": "Test IdP",
        "preset": OIDCPreset.KEYCLOAK,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scopes": ["openid", "email", "profile"],
        "groups_claim": "groups",
    }
    values.update(overrides)
    record = OIDCProviderRecord(**values)
    db.add(record)
    await db.commit()
    return record


@dataclass
class LoginResult:
    start: httpx.Response
    callback: httpx.Response | None

    @property
    def location(self) -> str:
        response = self.callback or self.start
        return response.headers["location"]


async def sign_in(
    oidc: OIDCTestApp,
    name: str = "test",
    *,
    return_to: str | None = None,
    tamper: Any = None,
) -> LoginResult:
    """Run the browser flow: start, IdP authorize (auto-consent), callback.

    ``tamper(callback_url) -> callback_url`` may change the callback before it is sent.
    """
    params = {"return_to": return_to} if return_to is not None else {}
    start = await oidc.client.get(f"/auth/oidc/{name}/login", params=params)
    if start.status_code != 303 or start.headers["location"].startswith("/"):
        return LoginResult(start=start, callback=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=oidc.idp.app())) as browser:
        authorize = await browser.get(start.headers["location"])
    assert authorize.status_code == 302
    callback_url = authorize.headers["location"]
    if tamper is not None:
        callback_url = tamper(callback_url)
    parts = urlsplit(callback_url)
    assert f"{parts.scheme}://{parts.netloc}" == PUBLIC_URL
    assert parts.path.startswith("/api/")
    callback = await oidc.client.get(parts.path.removeprefix("/api") + "?" + parts.query)
    return LoginResult(start=start, callback=callback)

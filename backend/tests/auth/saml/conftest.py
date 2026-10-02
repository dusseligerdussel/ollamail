"""Fixtures for SAML tests: a test IdP and the app with a database session."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.csrf import CSRF_HEADER
from app.auth.providers.saml.models import SAMLProviderRecord
from app.auth.providers.saml.presets import NAMEID_PERSISTENT
from app.core.config import AuthSettings, Settings
from app.core.db import get_db
from app.main import create_app
from tests.auth.saml.idp import FakeIdP, parse_authn_request
from tests.conftest import api_client

PUBLIC_URL = "https://mail.example.org"
ACS_URL = f"{PUBLIC_URL}/api/auth/saml/acs"


def sp_entity_id(name: str = "corp") -> str:
    return f"{PUBLIC_URL}/api/auth/saml/{name}/metadata"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"auth": AuthSettings(public_url=PUBLIC_URL)})


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@dataclass
class SAMLTestApp:
    app: FastAPI
    client: AsyncClient
    idp: FakeIdP


@pytest.fixture
async def saml(
    settings: Settings, db_session: AsyncSession, idp: FakeIdP
) -> AsyncIterator[SAMLTestApp]:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    test_app = SAMLTestApp(app=app, client=api_client(app), idp=idp)
    async with test_app.client:
        yield test_app
    await app.state.database.dispose()


async def add_provider(
    db: AsyncSession, idp: FakeIdP, name: str = "corp", **overrides: Any
) -> SAMLProviderRecord:
    values: dict[str, Any] = {
        "name": name,
        "display_name": "Corp SSO",
        "preset": "generic",
        "idp_entity_id": idp.entity_id,
        "idp_sso_url": idp.sso_url,
        "idp_certificates": [idp.keys.cert_b64],
        "name_id_format": NAMEID_PERSISTENT,
        "email_attribute": "email",
        "display_name_attribute": "displayName",
        "groups_attribute": "groups",
        "trust_email": True,
        "allowed_domains": [],
    }
    values.update(overrides)
    record = SAMLProviderRecord(**values)
    db.add(record)
    await db.commit()
    return record


# What a browser sends when the IdP's auto-submitting form posts to the ACS: a
# cross-site request without the CSRF header (a wrong one here, to prove the exemption).
CROSS_SITE = {"Sec-Fetch-Site": "cross-site", CSRF_HEADER: "none"}

DEFAULT_ATTRIBUTES = {
    "email": ["erika@example.org"],
    "displayName": ["Erika Mustermann"],
    "groups": ["staff", "mail-admins"],
}


@dataclass
class Started:
    response: httpx.Response
    request_id: str
    relay_state: str
    authn_request: str


async def start(saml: SAMLTestApp, name: str = "corp", return_to: str | None = None) -> Started:
    params = {"return_to": return_to} if return_to is not None else {}
    response = await saml.client.get(f"/auth/saml/{name}/login", params=params)
    assert response.status_code == 303, response.text
    request_id, relay_state, xml = parse_authn_request(response.headers["location"])
    return Started(response, request_id, relay_state, xml)


async def post_acs(
    saml: SAMLTestApp, saml_response: str, relay_state: str, **kwargs: Any
) -> httpx.Response:
    path = urlsplit(ACS_URL).path.removeprefix("/api")
    return await saml.client.post(
        path,
        data={"SAMLResponse": saml_response, "RelayState": relay_state},
        headers=CROSS_SITE,
        **kwargs,
    )


async def sign_in(
    saml: SAMLTestApp,
    name: str = "corp",
    *,
    return_to: str | None = None,
    attributes: dict[str, list[str]] | None = None,
    **response_args: Any,
) -> httpx.Response:
    """Start, answer at the test IdP, post to the ACS; the ACS response."""
    started = await start(saml, name, return_to)
    saml_response = saml.idp.response(
        in_response_to=started.request_id,
        acs_url=ACS_URL,
        audience=sp_entity_id(name),
        attributes=DEFAULT_ATTRIBUTES if attributes is None else attributes,
        **response_args,
    )
    return await post_acs(saml, saml_response, started.relay_state)


def error_of(response: httpx.Response) -> str | None:
    location = response.headers.get("location", "")
    return location.split("error=", 1)[1] if "error=" in location else None

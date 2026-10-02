"""SAML login against a real Keycloak (integration test).

Needs Keycloak with the admin account ``admin``/``admin`` at
``OLLAMAIL_TEST_KEYCLOAK_URL`` (default ``http://localhost:8180``), e.g.::

    docker run -p 8180:8080 -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \\
        -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin quay.io/keycloak/keycloak:26.6 start-dev

Skipped if unreachable; ``OLLAMAIL_TEST_REQUIRE_KEYCLOAK=1`` (CI) fails instead. Each test
gets its own realm with a SAML client, a user and a group, and drives the browser part
(Keycloak login form, auto-submitting response form) with httpx.
"""

import base64
import html
import os
import re
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Identity
from app.auth.providers.saml.models import SAMLProviderRecord
from app.users.models import User, UserRole
from tests.auth.conftest import login, make_local_user
from tests.auth.saml.conftest import ACS_URL, SAMLTestApp, error_of, post_acs, sp_entity_id

pytestmark = [pytest.mark.db, pytest.mark.keycloak]

KEYCLOAK_URL = os.environ.get("OLLAMAIL_TEST_KEYCLOAK_URL", "http://localhost:8180").rstrip("/")
REQUIRE_KEYCLOAK = os.environ.get("OLLAMAIL_TEST_REQUIRE_KEYCLOAK", "").lower() in {
    "1",
    "true",
    "yes",
}
PASSWORD = "Keycloak-Test-1"
_FORM_ACTION = re.compile(r'<form[^>]*id="kc-form-login"[^>]*action="([^"]+)"')
_HIDDEN = re.compile(r'<input[^>]*name="(SAMLResponse|RelayState)"[^>]*value="([^"]*)"')


class _PlainHttpCookies(httpx.AsyncBaseTransport):
    """Keycloak marks its cookies ``Secure`` even in development mode over plain HTTP;
    browsers keep them for localhost, httpx does not. Drop the flag for the test browser."""

    def __init__(self) -> None:
        self._transport = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        headers = [
            (name, re.sub(rb";\s*Secure", b"", value, flags=re.IGNORECASE))
            if name.lower() == b"set-cookie"
            else (name, value)
            for name, value in response.headers.raw
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=response.stream,
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


@dataclass
class Realm:
    name: str
    admin: AsyncClient

    @property
    def metadata_url(self) -> str:
        return f"{KEYCLOAK_URL}/realms/{self.name}/protocol/saml/descriptor"


async def _admin_client() -> AsyncClient:
    client = AsyncClient(base_url=KEYCLOAK_URL, timeout=30)
    try:
        token = await client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "admin",
                "password": "admin",
            },
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        message = f"Keycloak not reachable at {KEYCLOAK_URL} ({type(exc).__name__})"
        if REQUIRE_KEYCLOAK:
            pytest.fail(message)
        pytest.skip(message)
    token.raise_for_status()
    client.headers["Authorization"] = f"Bearer {token.json()['access_token']}"
    return client


@pytest.fixture
async def realm() -> AsyncIterator[Realm]:
    admin = await _admin_client()
    name = f"ollamail-{secrets.token_hex(4)}"
    (await admin.post("/admin/realms", json={"realm": name, "enabled": True})).raise_for_status()
    base = f"/admin/realms/{name}"
    try:
        (await admin.post(f"{base}/groups", json={"name": "mail-admins"})).raise_for_status()
        user = await admin.post(
            f"{base}/users",
            json={
                "username": "erika",
                "email": "erika@example.org",
                "emailVerified": True,
                "firstName": "Erika",
                "lastName": "Mustermann",
                "enabled": True,
                "groups": ["/mail-admins"],
                "credentials": [{"type": "password", "value": PASSWORD, "temporary": False}],
            },
        )
        user.raise_for_status()
        client = await admin.post(
            f"{base}/clients",
            json={
                "clientId": sp_entity_id(),
                "protocol": "saml",
                "enabled": True,
                "redirectUris": [ACS_URL],
                "attributes": {
                    "saml_assertion_consumer_url_post": ACS_URL,
                    "saml.server.signature": "true",
                    "saml.assertion.signature": "true",
                    "saml.client.signature": "false",
                    "saml.authnstatement": "true",
                    "saml_signature_algorithm": "RSA_SHA256",
                    "saml_name_id_format": "persistent",
                    "saml_force_name_id_format": "true",
                },
                "protocolMappers": [
                    _mapper("email", "saml-user-property-mapper", {"user.attribute": "email"}),
                    _mapper(
                        "displayName", "saml-user-property-mapper", {"user.attribute": "firstName"}
                    ),
                    _mapper(
                        "groups",
                        "saml-group-membership-mapper",
                        {"full.path": "false", "single": "false"},
                    ),
                ],
            },
        )
        client.raise_for_status()
        yield Realm(name=name, admin=admin)
    finally:
        await admin.delete(f"/admin/realms/{name}")
        await admin.aclose()


def _mapper(attribute: str, mapper: str, config: dict[str, str]) -> dict[str, object]:
    return {
        "name": attribute,
        "protocol": "saml",
        "protocolMapper": mapper,
        "config": {**config, "attribute.name": attribute, "attribute.nameformat": "Basic"},
    }


async def _configure(saml: SAMLTestApp, db: AsyncSession, realm: Realm) -> None:
    """Add the provider like an admin would: preset and metadata URL."""
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(saml.client, "admin@example.org")).status_code == 200
    created = await saml.client.post(
        "/admin/auth/saml/providers",
        json={
            "name": "corp",
            "display_name": "Keycloak",
            "preset": "keycloak",
            "metadata_url": realm.metadata_url,
            "trust_email": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["idp_entity_id"] == f"{KEYCLOAK_URL}/realms/{realm.name}"
    saml.client.cookies.clear()


async def _keycloak_response(saml: SAMLTestApp) -> tuple[str, str]:
    """Start the login at ollamail, sign in at Keycloak; the form Keycloak posts back."""
    start = await saml.client.get("/auth/saml/corp/login")
    assert start.status_code == 303
    assert start.headers["location"].startswith(KEYCLOAK_URL)
    async with AsyncClient(
        follow_redirects=True, timeout=30, transport=_PlainHttpCookies()
    ) as browser:
        page = await browser.get(start.headers["location"])
        assert page.status_code == 200, page.text
        action = _FORM_ACTION.search(page.text)
        assert action is not None, "Keycloak login form not found"
        answer = await browser.post(
            html.unescape(action.group(1)), data={"username": "erika", "password": PASSWORD}
        )
    assert answer.status_code == 200, answer.text
    fields = {name: html.unescape(value) for name, value in _HIDDEN.findall(answer.text)}
    assert f'action="{ACS_URL}"' in answer.text
    return fields["SAMLResponse"], fields["RelayState"]


async def test_keycloak_login(saml: SAMLTestApp, db_session: AsyncSession, realm: Realm) -> None:
    await _configure(saml, db_session, realm)
    saml_response, relay_state = await _keycloak_response(saml)

    response = await post_acs(saml, saml_response, relay_state)

    assert response.status_code == 303
    assert error_of(response) is None, response.headers["location"]
    user = await db_session.scalar(select(User).where(User.email == "erika@example.org"))
    assert user is not None
    assert user.display_name == "Erika"
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    assert identity.provider == "saml:corp"
    # Keycloak's persistent NameID: "G-<uuid>", stable per user and client.
    assert identity.subject.startswith("G-")
    assert "mail-admins" in identity.groups
    assert (await saml.client.get("/auth/me")).json()["email"] == "erika@example.org"


async def test_keycloak_response_cannot_be_replayed(
    saml: SAMLTestApp, db_session: AsyncSession, realm: Realm
) -> None:
    await _configure(saml, db_session, realm)
    saml_response, relay_state = await _keycloak_response(saml)
    flow_cookie = dict(saml.client.cookies)
    assert error_of(await post_acs(saml, saml_response, relay_state)) is None

    saml.client.cookies.clear()
    saml.client.cookies.update(flow_cookie)
    replayed = await post_acs(saml, saml_response, relay_state)

    assert error_of(replayed) == "invalid_response"


async def test_tampered_keycloak_response_is_rejected(
    saml: SAMLTestApp, db_session: AsyncSession, realm: Realm
) -> None:
    await _configure(saml, db_session, realm)
    saml_response, relay_state = await _keycloak_response(saml)
    xml = base64.b64decode(saml_response).decode()
    assert "erika@example.org" in xml
    tampered = base64.b64encode(xml.replace("erika@example.org", "admin@example.org").encode())

    response = await post_acs(saml, tampered.decode(), relay_state)

    assert error_of(response) == "invalid_response"
    assert (
        await db_session.scalar(select(User).where(User.email == "admin@example.org")) is not None
    )
    assert await db_session.scalar(select(Identity).where(Identity.provider == "saml:corp")) is None


async def test_keycloak_response_for_another_audience_is_rejected(
    saml: SAMLTestApp, db_session: AsyncSession, realm: Realm
) -> None:
    """Keycloak signs the response for the registered entity ID; ollamail now expects a
    different one (e.g. a second SP instance)."""
    await _configure(saml, db_session, realm)
    saml_response, relay_state = await _keycloak_response(saml)
    record = await db_session.scalar(
        select(SAMLProviderRecord).where(SAMLProviderRecord.name == "corp")
    )
    assert record is not None
    record.sp_entity_id = "https://other.example.org/sp"
    await db_session.commit()

    response = await post_acs(saml, saml_response, relay_state)

    assert error_of(response) == "invalid_response"

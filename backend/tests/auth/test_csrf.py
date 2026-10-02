from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.csrf import (
    CSRF_COOKIE,
    CSRF_ERROR_CODE,
    CSRF_HEADER,
    csrf_token_valid,
    issue_csrf_token,
)
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from tests.auth.conftest import PASSWORD, make_local_user

pytestmark = pytest.mark.db


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.post("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
async def plain(settings: Settings, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """A client without automatic CSRF handling."""
    app = _app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://test") as http:
        yield http
    await app.state.database.dispose()


async def _csrf_cookie(client: AsyncClient) -> str:
    await client.get("/healthz")
    return client.cookies[CSRF_COOKIE]


def test_token_is_bound_to_the_session(settings: Settings) -> None:
    anonymous = issue_csrf_token(settings, None)
    signed_in = issue_csrf_token(settings, "session-a")

    assert csrf_token_valid(settings, anonymous, None)
    assert csrf_token_valid(settings, signed_in, "session-a")
    assert not csrf_token_valid(settings, signed_in, "session-b")
    assert not csrf_token_valid(settings, anonymous, "session-a")
    assert not csrf_token_valid(settings, "forged.token", None)
    assert not csrf_token_valid(settings, None, None)
    assert issue_csrf_token(settings, None) != anonymous


async def test_safe_requests_receive_a_readable_cookie(plain: AsyncClient) -> None:
    response = await plain.get("/healthz")

    cookie = next(
        value.lower()
        for value in response.headers.get_list("set-cookie")
        if value.startswith(f"{CSRF_COOKIE}=")
    )
    assert "httponly" not in cookie
    assert "samesite=strict" in cookie
    assert "secure" in cookie
    # A valid cookie is not re-issued.
    assert "set-cookie" not in (await plain.get("/healthz")).headers


async def test_post_without_token_is_rejected(plain: AsyncClient) -> None:
    await _csrf_cookie(plain)

    response = await plain.post("/probe")

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF token missing or invalid."
    assert response.json()["error_code"] == CSRF_ERROR_CODE
    assert response.json()["request_id"] == response.headers["x-request-id"]


async def test_post_without_cookie_is_rejected_with_csrf_code(plain: AsyncClient) -> None:
    """What a browser on plain http:// sends: the Secure cookie was dropped (#142)."""
    response = await plain.post("/setup", json={}, headers={CSRF_HEADER: "anything"})

    assert response.status_code == 403
    assert response.json()["error_code"] == CSRF_ERROR_CODE


async def test_post_with_matching_token_passes(plain: AsyncClient) -> None:
    token = await _csrf_cookie(plain)

    response = await plain.post("/probe", headers={CSRF_HEADER: token})

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_all_unsafe_methods_are_checked(plain: AsyncClient, method: str) -> None:
    await _csrf_cookie(plain)

    response = await plain.request(method, "/auth/sessions")

    assert response.status_code == 403


async def test_wrong_header_is_rejected(plain: AsyncClient) -> None:
    token = await _csrf_cookie(plain)

    response = await plain.post("/probe", headers={CSRF_HEADER: token + "x"})

    assert response.status_code == 403


async def test_self_made_double_submit_pair_is_rejected(plain: AsyncClient) -> None:
    """An attacker who can plant cookies (e.g. from a sibling subdomain) cannot forge a
    token: it must be signed by the server."""
    plain.cookies.set(CSRF_COOKIE, "attacker.chosen")

    response = await plain.post("/probe", headers={CSRF_HEADER: "attacker.chosen"})

    assert response.status_code == 403


async def test_cross_site_requests_are_rejected(plain: AsyncClient) -> None:
    token = await _csrf_cookie(plain)

    response = await plain.post(
        "/probe", headers={CSRF_HEADER: token, "Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == CSRF_ERROR_CODE


async def test_login_rotates_the_token(plain: AsyncClient, db_session: AsyncSession) -> None:
    await make_local_user(db_session)
    anonymous = await _csrf_cookie(plain)

    response = await plain.post(
        "/auth/login",
        json={"email": "erika@example.org", "password": PASSWORD},
        headers={CSRF_HEADER: anonymous},
    )

    assert response.status_code == 200
    signed_in = plain.cookies[CSRF_COOKIE]
    assert signed_in != anonymous
    # The anonymous token no longer works with the new session.
    rejected = await plain.post("/probe", headers={CSRF_HEADER: anonymous})
    assert rejected.status_code == 403
    assert (await plain.post("/probe", headers={CSRF_HEADER: signed_in})).status_code == 200

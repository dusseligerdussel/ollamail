"""Integration tests: Gmail OAuth connect flow (API + PostgreSQL, Google mocked with respx)."""

import base64
import hashlib
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import GmailSettings, Settings
from app.core.crypto import KeyRing, decode_key, generate_key, set_keyring
from app.mail.models import Mailbox, MailboxType
from app.mail.providers.gmail_auth import SCOPE_MODIFY, SCOPE_READONLY, TOKEN_URL
from app.mail.providers.gmail_connect import STATE_COOKIE
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.mail.gmail_server import USER

pytestmark = pytest.mark.db

ADDRESS = "test.user@example.com"


@pytest.fixture(autouse=True)
def keyring() -> Iterator[None]:
    set_keyring(KeyRing(decode_key(generate_key())))
    yield
    set_keyring(None)


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "gmail": GmailSettings(
                client_id="client-id.apps.googleusercontent.com",
                client_secret="test-client-secret",  # type: ignore[arg-type]
                redirect_uri="https://test/api/mail/gmail/oauth/callback",
            )
        }
    )


@pytest.fixture
async def signed_in(db_session: AsyncSession, db_client: AsyncClient) -> AsyncClient:
    await make_local_user(db_session, "erika@example.org")
    assert (await login(db_client, "erika@example.org")).status_code == 200
    return db_client


@pytest.fixture
def google() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{USER}/profile").mock(
            return_value=httpx.Response(200, json={"emailAddress": ADDRESS, "historyId": "1"})
        )
        yield router


def token_response(scope: str = SCOPE_MODIFY, refresh_token: str | None = "rt-1") -> httpx.Response:
    body = {"access_token": "at", "expires_in": 3599, "scope": scope, "token_type": "Bearer"}
    if refresh_token:
        body["refresh_token"] = refresh_token
    return httpx.Response(200, json=body)


async def start(client: AsyncClient) -> dict[str, str]:
    response = await client.post("/mail/gmail/oauth/start", json={"login_hint": ADDRESS})
    assert response.status_code == 200
    url = urlparse(response.json()["authorization_url"])
    return {key: values[0] for key, values in parse_qs(url.query).items()}


async def mailboxes(session: AsyncSession) -> list[Mailbox]:
    rows = await session.scalars(
        select(Mailbox)
        .where(Mailbox.type == MailboxType.GMAIL)
        .execution_options(populate_existing=True)
    )
    return list(rows)


async def test_requires_sign_in(db_client: AsyncClient) -> None:
    assert (await db_client.post("/mail/gmail/oauth/start", json={})).status_code == 401
    assert (await db_client.get("/mail/gmail/oauth/callback")).status_code == 401


async def test_not_configured(
    signed_in: AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.gmail.client_id = None
    response = await signed_in.post("/mail/gmail/oauth/start", json={})
    assert response.status_code == 503


async def test_start_sets_state_cookie(signed_in: AsyncClient) -> None:
    params = await start(signed_in)

    assert params["login_hint"] == ADDRESS
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == "https://test/api/mail/gmail/oauth/callback"
    cookie = next(c for c in signed_in.cookies.jar if c.name == STATE_COOKIE)
    assert cookie.secure
    assert cookie.has_nonstandard_attr("HttpOnly")
    # Neither the PKCE verifier nor the state are readable from the URL alone.
    assert params["state"] not in params["code_challenge"]


async def test_callback_creates_mailbox(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    params = await start(signed_in)
    token = google.post(TOKEN_URL).mock(return_value=token_response())

    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "auth-code", "state": params["state"]}
    )

    assert response.status_code == 303
    [mailbox] = await mailboxes(db_session)
    assert response.headers["location"] == f"/?mailbox_connected={mailbox.id}"
    assert (mailbox.address, mailbox.display_name) == (ADDRESS, ADDRESS)
    assert mailbox.provider_settings == {"auth": "oauth"}
    assert mailbox.credentials == {"refresh_token": "rt-1"}
    # Stored encrypted.
    raw = await db_session.scalar(
        text("SELECT credentials FROM mail_mailboxes WHERE id = :id"), {"id": mailbox.id}
    )
    assert "rt-1" not in raw
    # PKCE: the verifier from the cookie matches the challenge sent to Google.
    sent = parse_qs(token.calls[0].request.content.decode())
    verifier = sent["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge.rstrip(b"=").decode() == params["code_challenge"]
    assert sent["code"] == ["auth-code"]
    assert STATE_COOKIE not in {c.name for c in signed_in.cookies.jar}
    event = (
        await db_session.execute(
            text(
                "SELECT actor_id, target_type, target_id, details FROM audit_events"
                " WHERE action = 'mailbox.created'"
            )
        )
    ).one()
    assert (event.target_type, event.target_id) == ("mailbox", str(mailbox.id))
    assert event.details == {"provider": "gmail"}


async def test_reconnect_updates_the_refresh_token(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    google.post(TOKEN_URL).mock(
        side_effect=[token_response(refresh_token="rt-1"), token_response(refresh_token="rt-2")]
    )
    for _ in range(2):
        params = await start(signed_in)
        response = await signed_in.get(
            "/mail/gmail/oauth/callback", params={"code": "c", "state": params["state"]}
        )
        assert response.status_code == 303

    [mailbox] = await mailboxes(db_session)
    assert mailbox.credentials == {"refresh_token": "rt-2"}


async def test_state_mismatch_is_rejected(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    await start(signed_in)
    token = google.post(TOKEN_URL).mock(return_value=token_response())

    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "c", "state": "forged"}
    )

    assert response.headers["location"] == "/?mailbox_error=invalid_state"
    assert not token.called
    assert await mailboxes(db_session) == []


async def test_callback_without_cookie(signed_in: AsyncClient) -> None:
    response = await signed_in.get("/mail/gmail/oauth/callback", params={"code": "c", "state": "s"})
    assert response.headers["location"] == "/?mailbox_error=invalid_state"


async def test_tampered_cookie(signed_in: AsyncClient, google: respx.MockRouter) -> None:
    params = await start(signed_in)
    token = google.post(TOKEN_URL).mock(return_value=token_response())
    value = signed_in.cookies[STATE_COOKIE]
    signed_in.cookies.delete(STATE_COOKIE)
    signed_in.cookies.set(STATE_COOKIE, value[:-2] + "xx")
    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "c", "state": params["state"]}
    )
    assert response.headers["location"] == "/?mailbox_error=invalid_state"
    assert not token.called


async def test_user_cancelled_at_google(signed_in: AsyncClient) -> None:
    params = await start(signed_in)
    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"error": "access_denied", "state": params["state"]}
    )
    assert response.headers["location"] == "/?mailbox_error=access_denied"


async def test_unticked_gmail_scope(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    params = await start(signed_in)
    google.post(TOKEN_URL).mock(return_value=token_response(scope=SCOPE_READONLY))

    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "c", "state": params["state"]}
    )

    assert response.headers["location"] == "/?mailbox_error=insufficient_scope"
    assert await mailboxes(db_session) == []


async def test_failed_code_exchange(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    params = await start(signed_in)
    google.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "c", "state": params["state"]}
    )

    assert response.headers["location"] == "/?mailbox_error=token_revoked"
    assert await mailboxes(db_session) == []


async def test_new_mailbox_needs_refresh_token(
    signed_in: AsyncClient, db_session: AsyncSession, google: respx.MockRouter
) -> None:
    params = await start(signed_in)
    google.post(TOKEN_URL).mock(return_value=token_response(refresh_token=None))

    response = await signed_in.get(
        "/mail/gmail/oauth/callback", params={"code": "c", "state": params["state"]}
    )

    assert response.headers["location"] == "/?mailbox_error=refresh_token_missing"
    assert await mailboxes(db_session) == []

"""API tests: Microsoft 365 connect flow and change notifications (database needed)."""

import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.core.config import DatabaseSettings, SecuritySettings, Settings
from app.core.crypto import generate_key, set_keyring
from app.mail.models import Mailbox, MailboxType
from app.mail.providers import graph_router
from app.mail.providers.graph_auth import code_challenge
from app.mail.providers.graph_router import FLOW_COOKIE, ConnectFlow, seal, unseal
from app.mail.providers.graph_webhook import client_state
from tests.auth.conftest import login, make_local_user
from tests.conftest import TEST_DATABASE_URL
from tests.mail.graph_helpers import GRAPH_HOST, TOKEN_URL, graph_settings, token_response

pytestmark = pytest.mark.db

NOTIFICATION_URL = "https://mail.example.com/api/mail/graph/notifications"


@pytest.fixture
def settings() -> Iterator[Settings]:
    value = Settings(
        database=DatabaseSettings.model_validate({"url": TEST_DATABASE_URL}),
        security=SecuritySettings.model_validate({"secret_key": generate_key()}),
        graph=graph_settings(notification_url=NOTIFICATION_URL),
    )
    # Mailbox credentials are encrypted with the process-wide key ring.
    set_keyring(None)
    from app.core.crypto import KeyRing, decode_key

    assert value.security.secret_key is not None
    set_keyring(KeyRing(decode_key(value.security.secret_key.get_secret_value())))
    yield value
    set_keyring(None)


@pytest.fixture
def requested() -> Iterator[list[uuid.UUID]]:
    calls: list[uuid.UUID] = []

    async def request_sync(mailbox_id: uuid.UUID) -> None:
        calls.append(mailbox_id)

    original = graph_router._request_sync
    graph_router._request_sync = request_sync  # type: ignore[assignment]
    yield calls
    graph_router._request_sync = original  # type: ignore[assignment]


async def signed_in(db_client: AsyncClient, db_session: AsyncSession, email: str) -> uuid.UUID:
    user = await make_local_user(db_session, email)
    response = await login(db_client, email)
    assert response.status_code == 200, response.text
    return user.id


async def start(db_client: AsyncClient, body: dict[str, Any] | None = None) -> dict[str, list[str]]:
    response = await db_client.post("/mail/graph/connect", json=body or {})
    assert response.status_code == 200, response.text
    assert FLOW_COOKIE in response.cookies
    url = response.json()["authorization_url"]
    assert url.startswith(
        "https://login.microsoftonline.com/00000000-0000-4000-8000-0000000000aa/oauth2/v2.0/authorize?"
    )
    return parse_qs(urlsplit(url).query)


def mock_microsoft(router: respx.MockRouter, *, me: dict[str, Any] | None = None) -> respx.Route:
    token = router.post(TOKEN_URL).mock(return_value=token_response("access-new", "refresh-new"))
    router.get(host=GRAPH_HOST, path="/v1.0/me").respond(
        json=me
        or {
            "id": "graph-user-1",
            "mail": "Erika@Example.com",
            "userPrincipalName": "erika@example.com",
            "displayName": "Erika Mustermann",
        }
    )
    return token


async def test_connect_flow_creates_the_mailbox(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    user_id = await signed_in(db_client, db_session, "erika@example.org")
    params = await start(db_client, {"return_to": "/settings/mailboxes"})

    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == ["https://test/mail/graph/callback"]
    scopes = params["scope"][0].split()
    assert "offline_access" in scopes
    assert "https://graph.microsoft.com/Mail.ReadWrite" in scopes
    assert "https://graph.microsoft.com/Mail.ReadWrite.Shared" not in scopes

    flow = unseal(settings, db_client.cookies[FLOW_COOKIE])
    assert flow is not None and flow.user_id == str(user_id)
    assert params["code_challenge"] == [code_challenge(flow.verifier)]

    with respx.mock(assert_all_called=True) as router:
        token = mock_microsoft(router)
        response = await db_client.get(
            "/mail/graph/callback", params={"code": "auth-code", "state": params["state"][0]}
        )

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    assert location.path == "/settings/mailboxes"
    query = parse_qs(location.query)
    assert query["graph"] == ["connected"]
    sent = dict(httpx.QueryParams(token.calls[0].request.content.decode()))
    assert sent["grant_type"] == "authorization_code"
    assert sent["code"] == "auth-code"
    assert sent["code_verifier"] == flow.verifier
    assert sent["redirect_uri"] == "https://test/mail/graph/callback"
    # The flow cookie is single-use.
    assert FLOW_COOKIE not in db_client.cookies

    mailbox = await db_session.get(Mailbox, uuid.UUID(query["mailbox_id"][0]))
    assert mailbox is not None
    assert mailbox.type is MailboxType.GRAPH
    assert mailbox.owner_user_id == user_id and not mailbox.is_shared
    assert mailbox.address == "erika@example.com"
    assert mailbox.display_name == "Erika Mustermann"
    assert mailbox.provider_settings == {
        "auth": "delegated",
        "user": "me",
        "user_id": "graph-user-1",
    }
    assert mailbox.credentials is not None
    assert mailbox.credentials["refresh_token"] == "refresh-new"
    assert mailbox.credentials["access_token"] == "access-new"

    events = (
        await db_session.execute(
            select(audit_events.c.action, audit_events.c.target_id, audit_events.c.details).where(
                audit_events.c.action == "mailbox.created"
            )
        )
    ).all()
    assert events == [("mailbox.created", str(mailbox.id), {"type": "graph", "shared": False})]


async def test_reconnecting_replaces_the_tokens(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    ids = []
    for _ in range(2):
        params = await start(db_client)
        with respx.mock() as router:
            mock_microsoft(router)
            response = await db_client.get(
                "/mail/graph/callback", params={"code": "c", "state": params["state"][0]}
            )
        ids.append(parse_qs(urlsplit(response.headers["location"]).query)["mailbox_id"][0])
    assert ids[0] == ids[1]
    count = len(list(await db_session.scalars(select(Mailbox.id))))
    assert count == 1


async def test_shared_mailbox_with_full_access(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    params = await start(db_client, {"shared_mailbox": "Team@Example.com"})
    assert "https://graph.microsoft.com/Mail.ReadWrite.Shared" in params["scope"][0].split()

    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        inbox = router.get(
            host=GRAPH_HOST, path="/v1.0/users/team@example.com/mailFolders/inbox"
        ).respond(json={"id": "inbox"})
        response = await db_client.get(
            "/mail/graph/callback", params={"code": "c", "state": params["state"][0]}
        )
    assert inbox.called
    mailbox_id = parse_qs(urlsplit(response.headers["location"]).query)["mailbox_id"][0]
    mailbox = await db_session.get(Mailbox, uuid.UUID(mailbox_id))
    assert mailbox is not None and mailbox.address == "team@example.com"
    assert mailbox.provider_settings == {"auth": "delegated", "user": "team@example.com"}


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ({"code": "c", "state": "wrong"}, "state_invalid"),
        ({"code": "c"}, "state_invalid"),
        ({"error": "access_denied", "state": None}, "consent_denied"),
    ],
)
async def test_callback_rejects_bad_requests(
    db_client: AsyncClient,
    db_session: AsyncSession,
    settings: Settings,
    query: dict[str, Any],
    reason: str,
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    params = await start(db_client)
    if "state" in query and query["state"] is None:
        query["state"] = params["state"][0]
    with respx.mock(assert_all_called=False) as router:
        token = router.post(TOKEN_URL).mock(return_value=token_response())
        response = await db_client.get("/mail/graph/callback", params=query)
    assert response.status_code == 303
    assert parse_qs(urlsplit(response.headers["location"]).query) == {
        "graph": ["error"],
        "reason": [reason],
    }
    assert not token.called
    assert list(await db_session.scalars(select(Mailbox.id))) == []


async def test_callback_needs_the_user_who_started_the_flow(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    params = await start(db_client)
    flow_cookie = db_client.cookies[FLOW_COOKIE]
    await db_client.post("/auth/logout")
    await signed_in(db_client, db_session, "max@example.org")
    db_client.cookies.set(FLOW_COOKIE, flow_cookie)

    response = await db_client.get(
        "/mail/graph/callback", params={"code": "c", "state": params["state"][0]}
    )
    assert parse_qs(urlsplit(response.headers["location"]).query)["reason"] == ["session_mismatch"]


async def test_revoked_consent_is_reported(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    params = await start(db_client)
    with respx.mock() as router:
        router.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
        response = await db_client.get(
            "/mail/graph/callback", params={"code": "c", "state": params["state"][0]}
        )
    assert parse_qs(urlsplit(response.headers["location"]).query)["reason"] == [
        "token_exchange_failed"
    ]


async def test_connect_requires_sign_in(db_client: AsyncClient, settings: Settings) -> None:
    response = await db_client.post("/mail/graph/connect", json={})
    assert response.status_code == 401


async def test_connect_without_configuration_is_not_found(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.graph.client_id = None
    await signed_in(db_client, db_session, "erika@example.org")
    response = await db_client.post("/mail/graph/connect", json={})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "value", ["https://evil.example/", "//evil.example/x", "/\\evil", "relative", None]
)
def test_return_to_stays_on_this_site(value: str | None) -> None:
    assert graph_router.safe_return_to(value) == "/"
    assert graph_router.safe_return_to("/inbox?x=1") == "/inbox?x=1"


def test_flow_cookie_is_authenticated_and_expires(settings: Settings) -> None:
    flow = ConnectFlow(
        state="s",
        verifier="v",
        user_id=str(uuid.uuid4()),
        redirect_uri="https://test/cb",
        return_to="/",
        shared_mailbox=None,
        expires_at=1000,
    )
    sealed = seal(settings, flow)
    assert unseal(settings, sealed, now=999) == flow
    assert unseal(settings, sealed, now=1001) is None
    tampered = sealed[:-2] + ("A" if sealed[-2] != "A" else "B") + sealed[-1]
    assert unseal(settings, tampered, now=0) is None
    assert unseal(settings, "garbage", now=0) is None
    other = Settings(security=SecuritySettings.model_validate({"secret_key": generate_key()}))
    assert unseal(other, sealed, now=0) is None


# -- notifications --------------------------------------------------------------------------


async def test_subscription_validation_echoes_the_token(
    db_client: AsyncClient, settings: Settings
) -> None:
    response = await db_client.post(
        "/mail/graph/notifications",
        params={"validationToken": "Validation: Token 123"},
        headers={"X-CSRF-Token": ""},
    )
    assert response.status_code == 200
    assert response.text == "Validation: Token 123"
    assert response.headers["content-type"].startswith("text/plain")


async def test_notifications_queue_a_sync_of_verified_mailboxes(
    db_client: AsyncClient, settings: Settings, requested: list[uuid.UUID]
) -> None:
    mailbox_id = uuid.uuid4()
    valid = client_state(settings.security, mailbox_id)
    forged = f"{uuid.uuid4()}.{valid.split('.')[1]}"
    notifications = {
        "value": [
            {
                "subscriptionId": "sub-1",
                "clientState": valid,
                "changeType": "created",
                "resource": "Users/x/Messages/y",
            },
            {"subscriptionId": "sub-1", "clientState": valid, "changeType": "updated"},
            {"subscriptionId": "sub-1", "clientState": valid, "lifecycleEvent": "missed"},
            {"subscriptionId": "sub-2", "clientState": forged, "changeType": "created"},
            {"subscriptionId": "sub-3", "changeType": "created"},
        ]
    }
    # Sent by Microsoft: no cookies, no CSRF token.
    response = await db_client.post(
        "/mail/graph/notifications", json=notifications, headers={"X-CSRF-Token": ""}
    )
    assert response.status_code == 202
    assert requested == [mailbox_id]


async def test_garbage_notifications_are_accepted_and_ignored(
    db_client: AsyncClient, settings: Settings, requested: list[uuid.UUID]
) -> None:
    for body in [b"not json", b"[]", b'{"value": "x"}', b'{"value": [1, null]}']:
        response = await db_client.post(
            "/mail/graph/notifications",
            content=body,
            headers={"X-CSRF-Token": "", "Content-Type": "application/json"},
        )
        assert response.status_code == 202
    assert requested == []


async def test_oversized_notifications_are_rejected(
    db_client: AsyncClient, settings: Settings, requested: list[uuid.UUID]
) -> None:
    response = await db_client.post(
        "/mail/graph/notifications",
        content=b" " * (1024 * 1024 + 1),
        headers={"X-CSRF-Token": "", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert requested == []


async def test_notifications_are_off_without_a_public_url(
    db_client: AsyncClient, settings: Settings
) -> None:
    settings.graph.notification_url = None
    response = await db_client.post(
        "/mail/graph/notifications", params={"validationToken": "x"}, headers={"X-CSRF-Token": ""}
    )
    assert response.status_code == 404


async def test_csrf_still_protects_other_endpoints(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await signed_in(db_client, db_session, "erika@example.org")
    response = await db_client.post(
        "/mail/graph/connect", json={}, headers={"X-CSRF-Token": "forged"}
    )
    assert response.status_code == 403

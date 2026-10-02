"""Contract tests of ``GraphProvider.send`` against synthetic Microsoft Graph responses
(``createReply``/``createReplyAll`` and ``send``, Microsoft Graph v1.0)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import respx

from app.mail import compose
from app.mail.providers.base import (
    ConnectionFailedError,
    MessageNotFoundError,
    OutgoingAddress,
    OutgoingReply,
    SendError,
)
from app.mail.providers.graph_auth import clear_app_token_cache
from tests.mail.graph_helpers import (
    GRAPH_HOST,
    TOKEN_URL,
    form,
    graph_error,
    graph_settings,
    make_provider,
    token_response,
)

SENDER = OutgoingAddress("erika@example.com", "Erika Mustermann")


@pytest.fixture
def graph() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture(autouse=True)
def _app_tokens() -> Iterator[None]:
    clear_app_token_cache()
    yield
    clear_app_token_cache()


def path(suffix: str) -> str:
    return f"/v1.0{suffix}"


def reply(*, reply_all: bool = False, ref: str | None = "m1") -> OutgoingReply:
    to = (OutgoingAddress("max@example.com", "Max Mustermann"),)
    cc = (OutgoingAddress("team@example.com"),) if reply_all else ()
    raw, message_id = compose.build_reply(
        sender=SENDER,
        to=to,
        cc=cc,
        subject="Re: Angebot",
        body="Hallo Max,\n\ndanke!",
        in_reply_to="<orig@example.com>",
        references=["<orig@example.com>"],
        date=datetime(2026, 10, 2, tzinfo=UTC),
    )
    return OutgoingReply(
        raw=raw,
        sender=SENDER,
        to=to,
        cc=cc,
        subject="Re: Angebot",
        body_text="Hallo Max,\n\ndanke!",
        message_id=message_id,
        in_reply_to_ref=ref,
        provider_thread_id="conv-1",
        reply_all=reply_all,
    )


def draft(draft_id: str = "d1") -> dict[str, Any]:
    return {"id": draft_id, "internetMessageId": "<graph-1@example.com>", "isDraft": True}


async def test_reply_is_created_on_the_original_and_sent(graph: respx.MockRouter) -> None:
    token = graph.post(TOKEN_URL).mock(return_value=token_response(access="send-token"))
    create = graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply")).respond(
        201, json=draft()
    )
    send = graph.post(host=GRAPH_HOST, path=path("/me/messages/d1/send")).respond(202)
    provider = make_provider()

    result = await provider.send(reply())
    await provider.aclose()

    # The stored token was issued for the sync: a token with Mail.Send is requested.
    scopes = form(token.calls[0].request)["scope"].split()
    assert "https://graph.microsoft.com/Mail.Send" in scopes
    assert "https://graph.microsoft.com/Mail.ReadWrite" in scopes
    body = json.loads(create.calls[0].request.content)
    assert body == {
        "message": {
            "subject": "Re: Angebot",
            "toRecipients": [
                {"emailAddress": {"address": "max@example.com", "name": "Max Mustermann"}}
            ],
            "ccRecipients": [],
            "body": {"contentType": "text", "content": "Hallo Max,\n\ndanke!"},
        }
    }
    assert create.calls[0].request.headers["Authorization"] == "Bearer send-token"
    assert send.call_count == 1
    assert send.calls[0].request.headers["Authorization"] == "Bearer send-token"
    assert (result.remote_ref, result.message_id) == ("d1", "<graph-1@example.com>")


async def test_reply_all_uses_create_reply_all(graph: respx.MockRouter) -> None:
    graph.post(TOKEN_URL).mock(return_value=token_response())
    create = graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReplyAll")).respond(
        201, json=draft()
    )
    graph.post(host=GRAPH_HOST, path=path("/me/messages/d1/send")).respond(202)
    provider = make_provider()

    await provider.send(reply(reply_all=True))
    await provider.aclose()

    body = json.loads(create.calls[0].request.content)["message"]
    assert body["ccRecipients"] == [{"emailAddress": {"address": "team@example.com"}}]


async def test_shared_mailbox_requests_send_shared(graph: respx.MockRouter) -> None:
    token = graph.post(TOKEN_URL).mock(return_value=token_response())
    graph.post(
        host=GRAPH_HOST, path=path("/users/team@example.com/messages/m1/createReply")
    ).respond(201, json=draft())
    graph.post(host=GRAPH_HOST, path=path("/users/team@example.com/messages/d1/send")).respond(202)
    # Another user's mailbox, opened with the signed-in user's delegated token.
    provider = make_provider(
        settings={"auth": "delegated", "user": "team@example.com"}, address="erika@example.com"
    )

    await provider.send(reply())
    await provider.aclose()

    scopes = form(token.calls[0].request)["scope"].split()
    assert "https://graph.microsoft.com/Mail.Send.Shared" in scopes


async def test_app_only_mailbox_sends_with_the_app_token(graph: respx.MockRouter) -> None:
    token = graph.post(TOKEN_URL).mock(return_value=token_response(access="app-token"))
    graph.post(
        host=GRAPH_HOST, path=path("/users/team@example.com/messages/m1/createReply")
    ).respond(201, json=draft())
    send = graph.post(
        host=GRAPH_HOST, path=path("/users/team@example.com/messages/d1/send")
    ).respond(202)
    provider = make_provider(settings={"auth": "application"}, address="team@example.com", creds={})

    await provider.send(reply())
    await provider.aclose()

    assert form(token.calls[0].request)["grant_type"] == "client_credentials"
    assert send.calls[0].request.headers["Authorization"] == "Bearer app-token"


async def test_missing_send_permission(graph: respx.MockRouter) -> None:
    graph.post(TOKEN_URL).mock(return_value=token_response())
    graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply")).respond(201, json=draft())
    graph.post(host=GRAPH_HOST, path=path("/me/messages/d1/send")).mock(
        return_value=graph_error(403, "ErrorAccessDenied")
    )
    delete = graph.delete(host=GRAPH_HOST, path=path("/me/messages/d1")).respond(204)
    provider = make_provider()

    with pytest.raises(SendError) as caught:
        await provider.send(reply())
    await provider.aclose()

    assert caught.value.code == "send_not_permitted"
    assert "erika" not in str(caught.value)
    # The reply draft created on the server is removed again.
    assert delete.call_count == 1


async def test_consent_missing_for_the_send_scope(graph: respx.MockRouter) -> None:
    graph.post(TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    create = graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply"))
    provider = make_provider()

    with pytest.raises(SendError) as caught:
        await provider.send(reply())
    await provider.aclose()

    assert caught.value.code == "send_not_permitted"
    assert create.call_count == 0


async def test_sending_disabled_by_the_admin(graph: respx.MockRouter) -> None:
    create = graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply"))
    provider = make_provider(graph=graph_settings(send_enabled=False))

    with pytest.raises(SendError) as caught:
        await provider.send(reply())
    await provider.aclose()

    assert caught.value.code == "send_not_permitted"
    assert create.call_count == 0


async def test_original_deleted_on_the_server(graph: respx.MockRouter) -> None:
    graph.post(TOKEN_URL).mock(return_value=token_response())
    graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply")).mock(
        return_value=graph_error(404, "ErrorItemNotFound")
    )
    provider = make_provider()

    with pytest.raises(MessageNotFoundError):
        await provider.send(reply())
    await provider.aclose()


async def test_send_needs_the_original(graph: respx.MockRouter) -> None:
    provider = make_provider()

    with pytest.raises(SendError) as caught:
        await provider.send(reply(ref=None))
    await provider.aclose()

    assert caught.value.code == "original_missing"


async def test_throttled_send_is_not_repeated(graph: respx.MockRouter) -> None:
    graph.post(TOKEN_URL).mock(return_value=token_response())
    graph.post(host=GRAPH_HOST, path=path("/me/messages/m1/createReply")).respond(201, json=draft())
    send = graph.post(host=GRAPH_HOST, path=path("/me/messages/d1/send")).mock(
        return_value=graph_error(503, "ServiceUnavailable", {"Retry-After": "1"})
    )
    graph.delete(host=GRAPH_HOST, path=path("/me/messages/d1")).respond(204)
    provider = make_provider()

    with pytest.raises(ConnectionFailedError):
        await provider.send(reply())
    await provider.aclose()

    assert send.call_count == 1


def test_connect_flow_requests_mail_send() -> None:
    from app.mail.providers.graph_auth import delegated_scopes

    settings = graph_settings()
    assert "https://graph.microsoft.com/Mail.Send" in delegated_scopes(settings, send=True)
    assert "https://graph.microsoft.com/Mail.Send.Shared" in delegated_scopes(
        settings, shared=True, send=True
    )
    # The sync keeps its scopes: mailboxes connected earlier continue to work.
    assert not any("Mail.Send" in s for s in delegated_scopes(settings, shared=True))

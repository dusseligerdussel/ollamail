"""Contract tests of ``GmailProvider.send`` (``messages.send``) against the synthetic Gmail
API of ``gmail_server``."""

import base64
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.config import GmailSettings, MailSettings
from app.mail import compose
from app.mail.models import MailboxType
from app.mail.providers.base import (
    ConnectionFailedError,
    MailboxConfig,
    OutgoingAddress,
    OutgoingReply,
    ProviderError,
    SendError,
)
from app.mail.providers.gmail import GmailProvider
from tests.mail.gmail_server import Fault, GmailServer, StaticTokens, google_error

ADDRESS = "test.user@example.com"
SENDER = OutgoingAddress(ADDRESS, "Test User")


async def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def server() -> Iterator[GmailServer]:
    gmail = GmailServer(address=ADDRESS)
    with respx.mock(assert_all_called=False) as router:
        gmail.install(router)
        yield gmail


def make_provider(*, readonly: bool = False) -> GmailProvider:
    config = MailboxConfig(
        mailbox_id="mb-1",
        type=MailboxType.GMAIL,
        address=ADDRESS,
        credentials={"refresh_token": "test-refresh-token"},
    )
    return GmailProvider(
        config,
        settings=GmailSettings(readonly=readonly),
        mail_settings=MailSettings(),
        token_source=StaticTokens(),
        sleep=no_sleep,
    )


@pytest.fixture
async def provider(server: GmailServer) -> AsyncIterator[GmailProvider]:
    gmail = make_provider()
    yield gmail
    await gmail.aclose()


def reply(thread_id: str | None) -> OutgoingReply:
    to = (OutgoingAddress("sender@example.com", "Sender"),)
    raw, message_id = compose.build_reply(
        sender=SENDER,
        to=to,
        cc=(),
        subject="Re: Synthetic message 1",
        body="Thanks, works for me.",
        in_reply_to="<synthetic-1@example.com>",
        references=["<synthetic-1@example.com>"],
        date=datetime(2026, 10, 2, tzinfo=UTC),
    )
    return OutgoingReply(
        raw=raw,
        sender=SENDER,
        to=to,
        subject="Re: Synthetic message 1",
        body_text="Thanks, works for me.",
        message_id=message_id,
        in_reply_to_ref="18f0000000000001",
        provider_thread_id=thread_id,
    )


async def test_reply_is_sent_in_the_thread_of_the_original(
    server: GmailServer, provider: GmailProvider
) -> None:
    original = server.add_message(b"Subject: Synthetic message 1\r\n\r\nHi\r\n", thread_id="t-1")
    outgoing = reply(server.messages[original].thread_id)

    result = await provider.send(outgoing)

    [request] = [r for r in server.requests if r.url.path.endswith("/messages/send")]
    body = json.loads(request.content)
    assert body["threadId"] == "t-1"
    assert base64.urlsafe_b64decode(body["raw"]) == outgoing.raw
    [sent] = server.sent
    assert result.remote_ref == sent
    assert server.messages[sent].thread_id == "t-1"
    assert server.messages[sent].label_ids == ["SENT"]
    raw = server.messages[sent].raw
    assert b"In-Reply-To: <synthetic-1@example.com>" in raw
    assert b"References: <synthetic-1@example.com>" in raw


async def test_reply_without_server_thread(server: GmailServer, provider: GmailProvider) -> None:
    await provider.send(reply(None))

    [request] = [r for r in server.requests if r.url.path.endswith("/messages/send")]
    assert "threadId" not in json.loads(request.content)


async def test_read_only_instances_do_not_send(server: GmailServer) -> None:
    gmail = make_provider(readonly=True)

    with pytest.raises(ProviderError) as caught:
        await gmail.send(reply(None))
    await gmail.aclose()

    assert caught.value.code == "read_only"
    assert server.sent == []


async def test_missing_scope(server: GmailServer, provider: GmailProvider) -> None:
    server.faults.append(
        Fault("POST", "/messages/send$", google_error(403, "insufficientPermissions"))
    )

    with pytest.raises(SendError) as caught:
        await provider.send(reply(None))

    assert caught.value.code == "send_not_permitted"


async def test_rejected_message(server: GmailServer, provider: GmailProvider) -> None:
    server.faults.append(
        Fault("POST", "/messages/send$", google_error(400, "invalidArgument", "Invalid To header"))
    )

    with pytest.raises(SendError) as caught:
        await provider.send(reply(None))

    assert caught.value.code == "message_refused"
    assert "Invalid" not in str(caught.value)


async def test_server_errors_are_not_retried(server: GmailServer, provider: GmailProvider) -> None:
    server.faults.append(Fault("POST", "/messages/send$", google_error(500, "backendError"), 5))

    with pytest.raises(ConnectionFailedError):
        await provider.send(reply(None))

    assert len([r for r in server.requests if r.url.path.endswith("/messages/send")]) == 1
    assert server.sent == []


async def test_network_errors_are_not_retried(server: GmailServer, provider: GmailProvider) -> None:
    server.faults.append(Fault("POST", "/messages/send$", httpx.ConnectError("down"), 5))

    with pytest.raises(ConnectionFailedError):
        await provider.send(reply(None))

    assert len([r for r in server.requests if r.url.path.endswith("/messages/send")]) == 1

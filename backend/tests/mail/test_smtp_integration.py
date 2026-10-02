"""Sending replies of IMAP mailboxes: SMTP submission against Mailpit (``smtp_server``) and
the copy in "Sent" against Dovecot (``imap_server``)."""

import uuid
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default
from typing import Any

import pytest

from app.core.config import MailSettings
from app.mail import compose
from app.mail.models import FolderRole, MailboxType
from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    MailboxConfig,
    MessageFetched,
    OutgoingAddress,
    OutgoingReply,
    SendError,
)
from app.mail.providers.imap import ImapProvider
from app.mail.providers.smtp import SmtpTarget, _xoauth2, submit
from tests.mail import smtp_server
from tests.mail.imap_server import INSECURE, TestAccount

SENDER = OutgoingAddress("erika@example.org", "Erika Mustermann")
TO = (OutgoingAddress("max@example.org", "Max Mustermann"),)
CC = (OutgoingAddress("team@example.org"),)


def reply(sender: OutgoingAddress = SENDER, **overrides: Any) -> OutgoingReply:
    original = f"<orig-{uuid.uuid4().hex}@example.org>"
    raw, message_id = compose.build_reply(
        sender=sender,
        to=TO,
        cc=CC,
        subject="Re: Projekt Übergabe",
        body="Hallo Max,\n\ngerne, bis Freitag.\n\nErika",
        in_reply_to=original,
        references=["<root@example.org>", original],
        date=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
    )
    values: dict[str, Any] = {
        "raw": raw,
        "sender": sender,
        "to": TO,
        "cc": CC,
        "subject": "Re: Projekt Übergabe",
        "body_text": "Hallo Max",
        "message_id": message_id,
        "in_reply_to_ref": "1:1:INBOX",
    }
    values.update(overrides)
    return OutgoingReply(**values)


def target(**overrides: Any) -> SmtpTarget:
    values: dict[str, Any] = {
        "host": smtp_server.HOST,
        "port": smtp_server.PORT,
        "security": "starttls",
        "verify_certificate": False,
        "username": "erika@example.org",
        "auth": "password",
        "secret": "any",
        "timeout": 10,
    }
    values.update(overrides)
    return SmtpTarget(**values)


@pytest.mark.smtp
async def test_submit_delivers_reply_with_threading_headers() -> None:
    outgoing = reply()

    refused = await submit(target(), SENDER.address, outgoing.recipients, outgoing.raw)

    assert refused == 0
    received = await smtp_server.find(outgoing.message_id.strip("<>"))
    assert received["Subject"] == "Re: Projekt Übergabe"
    assert [a["Address"] for a in received["To"]] == ["max@example.org"]
    assert [a["Address"] for a in received["Cc"]] == ["team@example.org"]
    headers = await smtp_server.headers(received["ID"])
    original = headers["In-Reply-To"][0]
    assert headers["References"][0].split() == ["<root@example.org>", original]
    parsed = message_from_bytes(await smtp_server.raw(received["ID"]), policy=default)
    body = parsed.get_body(("plain",))
    assert body is not None
    assert "gerne, bis Freitag." in body.get_content()


def test_xoauth2_initial_response() -> None:
    assert _xoauth2("erika@example.org", "token") == (
        "user=erika@example.org\x01auth=Bearer token\x01\x01"
    )


@pytest.mark.smtp
async def test_implicit_tls_against_a_starttls_port_fails_cleanly() -> None:
    outgoing = reply()

    with pytest.raises(ConnectionFailedError):
        await submit(target(security="tls"), SENDER.address, outgoing.recipients, outgoing.raw)


@pytest.mark.smtp
async def test_certificate_is_verified_by_default() -> None:
    outgoing = reply()

    with pytest.raises(ConnectionFailedError) as caught:
        await submit(
            target(verify_certificate=True), SENDER.address, outgoing.recipients, outgoing.raw
        )
    assert caught.value.code == "tls_certificate_invalid"


async def test_submit_needs_recipients() -> None:
    with pytest.raises(SendError) as caught:
        await submit(target(), SENDER.address, [], b"")
    assert caught.value.code == "no_recipients"


async def test_unreachable_server_is_a_connection_error() -> None:
    with pytest.raises(ConnectionFailedError):
        await submit(
            target(host="127.0.0.1", port=1, timeout=2), "a@example.org", ["b@example.org"], b"x"
        )


def provider(
    settings: dict[str, Any], *, insecure: bool = True, **credentials: Any
) -> ImapProvider:
    config = MailboxConfig(
        mailbox_id=uuid.uuid4(),
        type=MailboxType.IMAP,
        address="erika@example.org",
        settings={"host": "imap.example.org", **settings},
        credentials=credentials or {"password": "secret"},
    )
    return ImapProvider(
        config, mail_settings=INSECURE if insecure else MailSettings(imap_timeout=5)
    )


async def test_unverified_smtp_certificates_need_the_admin_flag() -> None:
    imap = provider({"verify_certificate": True, "smtp_security": "none"}, insecure=False)

    with pytest.raises(ConfigurationError) as caught:
        await imap.send(reply())
    assert caught.value.code == "insecure_connection_refused"


async def test_smtp_needs_a_password() -> None:
    imap = provider({}, insecure=True, access_token="x")

    with pytest.raises(AuthenticationError) as caught:
        await imap.send(reply())
    assert caught.value.code == "credentials_missing"


async def test_smtp_defaults_follow_the_imap_settings() -> None:
    imap = provider({"username": "erika", "verify_certificate": False})

    smtp = await imap._smtp_target()

    assert (smtp.host, smtp.port, smtp.security) == ("imap.example.org", 587, "starttls")
    assert (smtp.username, smtp.secret, smtp.verify_certificate) == ("erika", "secret", False)
    custom = provider(
        {"smtp_host": "smtp.example.org", "smtp_security": "tls", "smtp_username": "e"},
        smtp_password="other",
    )
    smtp = await custom._smtp_target()
    assert (smtp.host, smtp.port, smtp.username, smtp.secret) == (
        "smtp.example.org",
        465,
        "e",
        "other",
    )


@pytest.mark.smtp
@pytest.mark.imap
async def test_imap_mailbox_sends_via_smtp_and_keeps_a_copy_in_sent(
    imap_account: TestAccount,
) -> None:
    sender = OutgoingAddress(imap_account.address, "Erika Mustermann")
    outgoing = reply(sender)
    imap = imap_account.provider(
        settings={
            "smtp_host": smtp_server.HOST,
            "smtp_port": smtp_server.PORT,
            "smtp_security": "starttls",
        }
    )
    try:
        result = await imap.send(outgoing)

        assert result.sent_copy_error is None
        assert result.remote_ref is not None
        assert (await smtp_server.find(outgoing.message_id.strip("<>")))["Subject"]
        folders = await imap.list_folders()
        sent = next(f for f in folders if f.role is FolderRole.SENT)
        events = [e async for e in imap.fetch_since(sent.remote_id, None)]
        [stored] = [e.message for e in events if isinstance(e, MessageFetched)]
        assert stored.raw.replace(b"\r\n", b"\n") == outgoing.raw.replace(b"\r\n", b"\n")
        assert "seen" in stored.flags
        assert stored.remote_ref == result.remote_ref
    finally:
        await imap.aclose()


@pytest.mark.smtp
@pytest.mark.imap
async def test_copy_in_sent_can_be_switched_off(imap_account: TestAccount) -> None:
    outgoing = reply(OutgoingAddress(imap_account.address))
    imap = imap_account.provider(
        settings={
            "smtp_host": smtp_server.HOST,
            "smtp_port": smtp_server.PORT,
            "smtp_save_sent": False,
        }
    )
    try:
        result = await imap.send(outgoing)

        assert result.remote_ref is None
        folders = await imap.list_folders()
        sent = [f for f in folders if f.role is FolderRole.SENT]
        for folder in sent:
            events = [e async for e in imap.fetch_since(folder.remote_id, None)]
            assert not [e for e in events if isinstance(e, MessageFetched)]
    finally:
        await imap.aclose()

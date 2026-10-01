"""Access to the IMAP test server for integration tests (marker ``imap``).

The tests run against Dovecot (``dovecot/dovecot`` image, the ``dovecot`` service in
``.github/workflows/ci.yml``). Its default configuration accepts every user name with the
password ``USER_PASSWORD`` and creates the mailbox on first login, so every test uses its
own fresh account and nothing needs cleaning up. Locally::

    docker run -d -p 31143:31143 -p 31993:31993 -e USER_PASSWORD=ollamail-test \\
        dovecot/dovecot:2.4.5

(On hosts without IPv6 append ``/dovecot/sbin/dovecot -F -o 'listen=*'``.)

Environment: ``OLLAMAIL_TEST_IMAP_HOST`` (``localhost``), ``OLLAMAIL_TEST_IMAP_PORT``
(implicit TLS, ``31993``), ``OLLAMAIL_TEST_IMAP_STARTTLS_PORT`` (``31143``),
``OLLAMAIL_TEST_IMAP_PASSWORD`` (``ollamail-test``). If the server is unreachable the tests
are skipped; ``OLLAMAIL_TEST_REQUIRE_IMAP=1`` (CI) fails instead.

Only synthetic messages (``example.org`` addresses) are used.
"""

import os
import socket
import ssl
import uuid
from datetime import datetime
from email.utils import format_datetime
from typing import Any

from app.core.config import MailSettings
from app.mail.models import MailboxType
from app.mail.providers.base import MailboxConfig
from app.mail.providers.imap import ImapProvider
from app.mail.providers.imap_client import ImapConnection, Literal_
from app.mail.providers.imap_protocol import encode_mailbox, flags_to_imap

HOST = os.environ.get("OLLAMAIL_TEST_IMAP_HOST", "localhost")
PORT = int(os.environ.get("OLLAMAIL_TEST_IMAP_PORT", "31993"))
STARTTLS_PORT = int(os.environ.get("OLLAMAIL_TEST_IMAP_STARTTLS_PORT", "31143"))
PASSWORD = os.environ.get("OLLAMAIL_TEST_IMAP_PASSWORD", "ollamail-test")
REQUIRE = os.environ.get("OLLAMAIL_TEST_REQUIRE_IMAP", "").lower() in {"1", "true", "yes"}

# The test server uses a self-signed certificate.
INSECURE = MailSettings(allow_insecure_connections=True, imap_timeout=20)


def probe() -> str | None:
    """``None`` if the server accepts TCP connections, else the error type."""
    try:
        with socket.create_connection((HOST, PORT), timeout=3):
            return None
    except OSError as exc:
        return type(exc).__name__


def message(
    number: int,
    *,
    subject: str | None = None,
    date: datetime | None = None,
    body: str | None = None,
) -> bytes:
    sent = format_datetime(date) if date else "Thu, 01 Oct 2026 10:00:00 +0000"
    return (
        f"From: Erika Mustermann <erika@example.org>\r\n"
        f"To: Max Mustermann <max@example.org>\r\n"
        f"Subject: {subject or f'Test message {number}'}\r\n"
        f"Message-ID: <test-{number}-{uuid.uuid4().hex}@example.org>\r\n"
        f"Date: {sent}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"\r\n"
        f"{body or f'Synthetic body {number}.'}\r\n"
    ).encode()


class TestAccount:
    """A fresh mailbox on the test server."""

    __test__ = False  # not a test class

    def __init__(self) -> None:
        self.address = f"test-{uuid.uuid4().hex[:12]}@example.org"
        self._admin: ImapConnection | None = None

    def config(self, **settings: Any) -> MailboxConfig:
        values: dict[str, Any] = {
            "host": HOST,
            "port": PORT,
            "security": "tls",
            "verify_certificate": False,
        }
        values.update(settings)
        password = values.pop("password", PASSWORD)
        return MailboxConfig(
            mailbox_id=uuid.uuid4(),
            type=MailboxType.IMAP,
            address=self.address,
            settings=values,
            credentials={"password": password},
        )

    def provider(self, *, settings: dict[str, Any] | None = None, **kwargs: Any) -> ImapProvider:
        kwargs.setdefault("mail_settings", INSECURE)
        return ImapProvider(self.config(**(settings or {})), **kwargs)

    async def admin(self) -> ImapConnection:
        """A separate connection that plays "other mail client" / incoming mail."""
        if self._admin is None or self._admin.closed:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = await ImapConnection.open(
                HOST,
                PORT,
                security="tls",
                ssl_context=context,
                connect_timeout=10,
                command_timeout=20,
            )
            await conn.login(self.address, PASSWORD)
            self._admin = conn
        return self._admin

    async def append(
        self,
        raw: bytes,
        folder: str = "INBOX",
        *,
        flags: frozenset[str] = frozenset(),
        received: datetime | None = None,
    ) -> None:
        conn = await self.admin()
        args: list[Any] = ["APPEND", encode_mailbox(folder), f"({' '.join(flags_to_imap(flags))})"]
        if received is not None:
            args.append(received.strftime("%d-%b-%Y %H:%M:%S %z").encode())
        await conn.command(*args, Literal_(raw))

    async def run(self, *parts: Any, folder: str | None = None) -> None:
        conn = await self.admin()
        if folder is not None:
            await conn.select(encode_mailbox(folder), read_only=False)
        await conn.command(*parts)

    async def close(self) -> None:
        if self._admin is not None:
            await self._admin.close()

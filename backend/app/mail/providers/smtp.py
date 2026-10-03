"""SMTP submission for IMAP mailboxes (``ImapProvider.send``).

Uses ``smtplib`` in a worker thread: one connection per message, closed right after.
Transport security as for IMAP: implicit TLS (port 465) or STARTTLS (587) with certificate
verification; plain connections and unverified certificates only with
``OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS``. Authentication with the mailbox password
(``AUTH PLAIN``/``LOGIN``) or an OAuth access token (``AUTH XOAUTH2``).

Errors become ``ProviderError``s with codes only; server replies (which may quote
addresses) are never logged or passed on.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.logging import get_logger
from app.mail.providers.base import (
    AuthenticationError,
    ConnectionFailedError,
    ProviderError,
    SendError,
)

log = get_logger(__name__)

SmtpSecurity = Literal["tls", "starttls", "none"]
DEFAULT_PORTS: dict[str, int] = {"tls": 465, "starttls": 587, "none": 25}


@dataclass(frozen=True, slots=True)
class SmtpTarget:
    host: str
    port: int
    security: SmtpSecurity
    verify_certificate: bool
    username: str
    auth: Literal["password", "xoauth2"]
    secret: str = field(repr=False)
    timeout: float = 60.0
    # Address checked by ``app.mail.providers.network``; connected to instead of resolving
    # ``host`` again (TLS still verifies the certificate for ``host``).
    address: str | None = None


def tls_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _xoauth2(username: str, token: str) -> str:
    return f"user={username}\x01auth=Bearer {token}\x01\x01"


class _PinnedSMTP(smtplib.SMTP):
    """Connects to ``address`` if given; ``host`` stays the TLS server name."""

    def __init__(self, *args: Any, address: str | None = None, **kwargs: Any) -> None:
        self._address = address
        super().__init__(*args, **kwargs)

    def connect(
        self, host: str = "localhost", port: int = 0, source_address: Any = None
    ) -> tuple[int, bytes]:
        return super().connect(self._address or host, port, source_address)


class _PinnedSMTPSSL(_PinnedSMTP, smtplib.SMTP_SSL):
    pass


def _open(target: SmtpTarget) -> smtplib.SMTP:
    context = tls_context(target.verify_certificate)
    if target.security == "tls":
        return _PinnedSMTPSSL(
            target.host,
            target.port,
            timeout=target.timeout,
            context=context,
            address=target.address,
        )
    client = _PinnedSMTP(target.host, target.port, timeout=target.timeout, address=target.address)
    try:
        client.ehlo()
        if target.security == "starttls":
            if not client.has_extn("starttls"):
                raise ConnectionFailedError(code="starttls_unsupported")
            client.starttls(context=context)
            client.ehlo()
    except BaseException:
        client.close()
        raise
    return client


def _submit(target: SmtpTarget, sender: str, recipients: list[str], raw: bytes) -> int:
    """Send and return the number of refused recipients (others got the message)."""
    try:
        with _open(target) as client:
            try:
                if target.auth == "xoauth2":
                    client.ehlo_or_helo_if_needed()
                    client.auth(
                        "XOAUTH2",
                        lambda challenge=None: _xoauth2(target.username, target.secret),
                        initial_response_ok=True,
                    )
                else:
                    client.login(target.username, target.secret)
            except smtplib.SMTPNotSupportedError:
                raise AuthenticationError(code="auth_unsupported") from None
            except smtplib.SMTPAuthenticationError:
                raise AuthenticationError() from None
            refused = client.sendmail(sender, recipients, raw)
            return len(refused)
    except ProviderError:
        raise
    except smtplib.SMTPRecipientsRefused:
        raise SendError(code="recipients_refused") from None
    except smtplib.SMTPSenderRefused:
        raise SendError(code="sender_refused") from None
    except smtplib.SMTPDataError:
        raise SendError(code="message_refused") from None
    except ssl.SSLCertVerificationError:
        raise ConnectionFailedError(code="tls_certificate_invalid") from None
    except ssl.SSLError:
        raise ConnectionFailedError(code="tls_failed") from None
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
        raise ConnectionFailedError() from None
    except smtplib.SMTPException:
        # Other server replies (``SMTPException`` is an ``OSError``, so before that).
        raise SendError() from None
    except OSError:
        # DNS, TCP, timeouts.
        raise ConnectionFailedError() from None


async def submit(target: SmtpTarget, sender: str, recipients: list[str], raw: bytes) -> int:
    """Submit ``raw`` for ``recipients``; returns the number of refused recipients."""
    if not recipients:
        raise SendError(code="no_recipients")
    refused = await asyncio.to_thread(_submit, target, sender, recipients, raw)
    if refused:
        log.warning("smtp_recipients_refused", refused=refused, recipient_count=len(recipients))
    return refused

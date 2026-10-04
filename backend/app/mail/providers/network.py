"""Destination check for the mail servers users enter (IMAP and SMTP host/port).

The check itself lives in ``app.core.network`` (shared with the CalDAV export): internal
addresses are refused unless ``OLLAMAIL_MAIL_ALLOWED_INTERNAL_HOSTS`` lists the host name or
a range containing the address (e.g. the IMAP server in the own LAN). Callers connect to the
returned address and keep the host name for TLS, so DNS rebinding cannot redirect the
connection. A refused destination raises the same ``ConnectionFailedError`` as an
unreachable one. Logs carry no host names or addresses (docs/PRIVACY.md).
"""

from app.core import network
from app.core.config import MailSettings
from app.core.logging import get_logger
from app.core.network import Allowlist, is_public
from app.mail.providers.base import ConnectionFailedError

__all__ = ["Allowlist", "is_public", "resolve"]

log = get_logger(__name__)


async def resolve(host: str, port: int, settings: MailSettings, *, seconds: float) -> str:
    """The address to connect to for ``host``: the first one that is public or allowed.

    Raises ``ConnectionFailedError`` if the name does not resolve within ``seconds`` or
    every address is internal and not on the allowlist.
    """
    allowlist = Allowlist.parse(settings.allowed_internal_hosts)
    try:
        return await network.resolve(host, port, allowlist, seconds=seconds)
    except network.DestinationError as exc:
        if exc.refused:
            log.warning("mail_destination_refused", port=port)
        raise ConnectionFailedError() from None

"""Which mailbox types a user can add, and how.

Types with a credentials form go through ``POST /mailboxes`` (connection test first).
OAuth types have their own connect flow; they are offered only if the provider is
registered and its OAuth client is configured. A new OAuth provider adds one entry to
``OAUTH_CONNECTORS``.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import Settings
from app.mail.api.message_schemas import MailboxProviderRead
from app.mail.models import MailboxType
from app.mail.providers.gmail_auth import oauth_configured
from app.mail.providers.registry import ProviderRegistry

# Added with server, user name and password (or token) in the form.
CREDENTIAL_TYPES = frozenset({MailboxType.IMAP})


@dataclass(frozen=True, slots=True)
class OAuthConnector:
    # API path (without ``/api``) that starts the flow and returns ``authorization_url``.
    start_path: str
    configured: Callable[[Settings], bool]


OAUTH_CONNECTORS: dict[MailboxType, OAuthConnector] = {
    MailboxType.GMAIL: OAuthConnector(
        "/mail/gmail/oauth/start", lambda settings: oauth_configured(settings.gmail)
    ),
}


def available(settings: Settings, providers: ProviderRegistry) -> list[MailboxProviderRead]:
    result: list[MailboxProviderRead] = []
    for type in MailboxType:
        if not providers.is_registered(type):
            continue
        connector = OAUTH_CONNECTORS.get(type)
        if connector is not None:
            if connector.configured(settings):
                result.append(
                    MailboxProviderRead(
                        type=type, connect="oauth", oauth_start_path=connector.start_path
                    )
                )
        elif type in CREDENTIAL_TYPES:
            result.append(MailboxProviderRead(type=type, connect="credentials"))
    return result

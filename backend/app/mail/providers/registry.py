"""Maps a mailbox type to a provider factory.

Provider modules register themselves at import time, e.g. in ``app/mail/providers/imap.py``::

    registry.register(MailboxType.IMAP, ImapProvider)

Features only ever call ``registry.create(config)``, so adding a provider needs no code
in features (CLAUDE.md §5.4).
"""

from collections.abc import Callable

from app.mail.models import MailboxType
from app.mail.providers.base import MailboxConfig, MailProvider

ProviderFactory = Callable[[MailboxConfig], MailProvider]


class UnknownProviderError(LookupError):
    pass


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[MailboxType, ProviderFactory] = {}

    def register(
        self, type: MailboxType, factory: ProviderFactory, *, replace: bool = False
    ) -> None:
        if type in self._factories and not replace:
            raise ValueError(f"a provider for {type.value!r} is already registered")
        self._factories[type] = factory

    def unregister(self, type: MailboxType) -> None:
        self._factories.pop(type, None)

    def is_registered(self, type: MailboxType) -> bool:
        return type in self._factories

    @property
    def types(self) -> frozenset[MailboxType]:
        return frozenset(self._factories)

    def create(self, config: MailboxConfig) -> MailProvider:
        try:
            factory = self._factories[config.type]
        except KeyError:
            raise UnknownProviderError(
                f"no provider registered for {config.type.value!r}"
            ) from None
        return factory(config)


registry = ProviderRegistry()

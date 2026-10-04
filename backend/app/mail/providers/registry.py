"""Maps a mailbox type to a provider factory.

Provider modules register themselves at import time, e.g. in ``app/mail/providers/imap.py``::

    registry.register(MailboxType.IMAP, ImapProvider)

Features only ever call ``registry.create(config)``, so adding a provider needs no code
in features (CLAUDE.md §5.4).

``destination`` names the ``provider_settings`` keys that decide where the stored
credentials are sent (host, port, transport security, token endpoint). Changing one of
them needs the credentials again (#219), so a stolen session cannot point a mailbox at
its own server and collect the password. Without it, every key counts.
"""

from collections.abc import Callable, Collection, Mapping
from typing import Any

from app.mail.models import MailboxType
from app.mail.providers.base import MailboxConfig, MailProvider

ProviderFactory = Callable[[MailboxConfig], MailProvider]


class UnknownProviderError(LookupError):
    pass


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[MailboxType, ProviderFactory] = {}
        self._destinations: dict[MailboxType, frozenset[str]] = {}

    def register(
        self,
        type: MailboxType,
        factory: ProviderFactory,
        *,
        destination: Collection[str] | None = None,
        replace: bool = False,
    ) -> None:
        if type in self._factories and not replace:
            raise ValueError(f"a provider for {type.value!r} is already registered")
        self._factories[type] = factory
        if destination is None:
            self._destinations.pop(type, None)
        else:
            self._destinations[type] = frozenset(destination)

    def unregister(self, type: MailboxType) -> None:
        self._factories.pop(type, None)
        self._destinations.pop(type, None)

    def destination_changed(
        self, type: MailboxType, old: Mapping[str, Any], new: Mapping[str, Any]
    ) -> bool:
        """Whether ``new`` settings send the credentials somewhere else than ``old``."""
        keys = self._destinations.get(type)
        if keys is None:
            keys = frozenset(old) | frozenset(new)
        return any(old.get(key) != new.get(key) for key in keys)

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

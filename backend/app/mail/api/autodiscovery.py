"""Connection suggestions for an address, from its domain.

Works offline: a table of common providers, then generic guesses (``imap.<domain>``,
``mail.<domain>``). No DNS or HTTP lookups, so nothing about the address leaves the
instance. Suggestions are only a starting point; the connection test decides.

Providers with their own API (Gmail, Microsoft 365) are suggested first once their
provider is registered (#37, #38); until then the IMAP settings are offered.
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.mail.models import MailboxType


class Hint(enum.StrEnum):
    """Machine-readable notes for the UI (translated there)."""

    # The provider requires an app-specific password for IMAP.
    APP_PASSWORD = "app_password"
    # IMAP access must be enabled in the provider's web settings first.
    ENABLE_IMAP = "enable_imap"
    # The provider no longer accepts passwords for IMAP; use its own provider type (OAuth).
    OAUTH_REQUIRED = "oauth_required"
    # The provider uses a separate password for mail clients.
    MAIL_PASSWORD = "mail_password"
    # Guessed from the domain, not a known provider.
    GUESSED = "guessed"


class Source(enum.StrEnum):
    KNOWN = "known"
    GUESS = "guess"


@dataclass(frozen=True, slots=True)
class Suggestion:
    type: MailboxType
    provider_settings: dict[str, Any]
    source: Source
    hints: tuple[Hint, ...] = ()


@dataclass(frozen=True, slots=True)
class _Preset:
    domains: frozenset[str]
    imap_host: str
    hints: tuple[Hint, ...] = ()
    # Provider type with its own API, preferred when registered.
    native: MailboxType | None = None


def _preset(domains: str, host: str, *hints: Hint, native: MailboxType | None = None) -> _Preset:
    return _Preset(frozenset(domains.split()), host, hints, native)


_PRESETS = (
    _preset(
        "gmail.com googlemail.com", "imap.gmail.com", Hint.APP_PASSWORD, native=MailboxType.GMAIL
    ),
    _preset(
        "outlook.com outlook.de hotmail.com hotmail.de live.com live.de msn.com",
        "outlook.office365.com",
        Hint.OAUTH_REQUIRED,
        native=MailboxType.GRAPH,
    ),
    _preset("icloud.com me.com mac.com", "imap.mail.me.com", Hint.APP_PASSWORD),
    _preset("yahoo.com yahoo.de ymail.com", "imap.mail.yahoo.com", Hint.APP_PASSWORD),
    _preset("aol.com aol.de", "imap.aol.com", Hint.APP_PASSWORD),
    _preset("gmx.de gmx.net gmx.at gmx.ch", "imap.gmx.net", Hint.ENABLE_IMAP),
    _preset("web.de", "imap.web.de", Hint.ENABLE_IMAP),
    _preset("t-online.de magenta.de", "secureimap.t-online.de", Hint.MAIL_PASSWORD),
    _preset("posteo.de posteo.net", "posteo.de"),
    _preset("mailbox.org", "imap.mailbox.org"),
    _preset("freenet.de", "mx.freenet.de", Hint.ENABLE_IMAP),
    _preset("fastmail.com fastmail.fm", "imap.fastmail.com", Hint.APP_PASSWORD),
)
_BY_DOMAIN = {domain: preset for preset in _PRESETS for domain in preset.domains}


def _imap(host: str) -> dict[str, Any]:
    return {"host": host, "port": 993, "security": "tls"}


def domain_of(address: str) -> str | None:
    _, at, domain = address.strip().rpartition("@")
    domain = domain.strip().rstrip(".").lower()
    if not at or not domain or "." not in domain:
        return None
    return domain


def suggest(address: str, is_registered: Callable[[MailboxType], bool]) -> list[Suggestion]:
    """Connection suggestions, best first; only provider types that are registered."""
    domain = domain_of(address)
    if domain is None:
        return []
    suggestions: list[Suggestion] = []
    preset = _BY_DOMAIN.get(domain)
    if preset is not None:
        if preset.native is not None and is_registered(preset.native):
            suggestions.append(Suggestion(preset.native, {}, Source.KNOWN))
        suggestions.append(
            Suggestion(MailboxType.IMAP, _imap(preset.imap_host), Source.KNOWN, preset.hints)
        )
    else:
        for prefix in ("imap", "mail"):
            suggestions.append(
                Suggestion(
                    MailboxType.IMAP, _imap(f"{prefix}.{domain}"), Source.GUESS, (Hint.GUESSED,)
                )
            )
    return [s for s in suggestions if is_registered(s.type)]

"""Interface for authentication providers.

A provider proves who someone is and returns a ``VerifiedIdentity``; it never creates
sessions or users itself. ``app.auth.service.user_for_identity`` maps the identity to a
user via the ``auth_identities`` table, and the login endpoint creates the session. That
way the local login (``LocalAuthProvider``), LDAP (#32), OIDC (#30) and GitHub (#31) share
lockout, session handling and role checks.

Two kinds of providers:

* ``PasswordAuthProvider`` checks a login name and password (local accounts, LDAP).
* ``RedirectAuthProvider`` sends the browser to an external IdP and validates the callback
  (OIDC, GitHub OAuth2). ``state``, ``nonce`` and the PKCE ``code_verifier`` are generated
  and checked by the caller.

Providers configured in the database (admin API) change at runtime and must look the same
on every API instance; they are added as a *source* (``AuthProviderRegistry.add_source``)
that is queried per request instead of being registered once at start-up.
"""

import enum
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


class AuthProviderKind(enum.StrEnum):
    PASSWORD = "password"
    REDIRECT = "redirect"


@dataclass(frozen=True)
class VerifiedIdentity:
    """An identity the provider has verified.

    ``provider`` and ``subject`` match ``Identity.provider``/``Identity.subject``; the
    subject must be stable (OIDC ``sub``, GitHub user ID, LDAP objectGUID), not an e-mail
    address. The remaining fields feed just-in-time provisioning and group→role mapping.
    """

    provider: str
    subject: str
    email: str | None = None
    display_name: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)
    # The provider vouches that ``email`` belongs to the person (OIDC ``email_verified``,
    # directory-managed address). Required for domain allowlists and account linking.
    email_verified: bool = False


@runtime_checkable
class AuthProvider(Protocol):
    # Unique key, stored in ``Identity.provider``: ``local``, ``ldap:<name>``, ``oidc:<name>``.
    name: str
    # Label for the login button / form.
    display_name: str
    kind: AuthProviderKind


@runtime_checkable
class PasswordAuthProvider(AuthProvider, Protocol):
    async def authenticate(self, login: str, password: str) -> VerifiedIdentity | None:
        """The verified identity, or ``None`` if the credentials are wrong.

        Must take about the same time whether or not the account exists.
        """
        ...


@runtime_checkable
class RedirectAuthProvider(AuthProvider, Protocol):
    # Path below the API root that starts the login, e.g. ``/auth/oidc/<name>/login``.
    login_path: str

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, code_verifier: str
    ) -> str:
        """URL of the IdP's login page (with the S256 PKCE challenge of ``code_verifier``)."""
        ...

    async def complete(
        self, *, params: Mapping[str, str], nonce: str, redirect_uri: str, code_verifier: str
    ) -> VerifiedIdentity:
        """Validate the callback parameters (code exchange, ID token) and return the
        identity. Raises on any validation error."""
        ...


ProviderSource = Callable[[AsyncSession], Awaitable[Iterable[AuthProvider]]]


class AuthProviderRegistry:
    """External providers configured for this instance.

    ``register`` adds a fixed provider; ``add_source`` adds a callable that returns the
    currently configured providers of one kind (e.g. OIDC providers from the database).
    ``available``/``find`` combine both.
    """

    def __init__(self) -> None:
        self._providers: dict[str, AuthProvider] = {}
        self._sources: list[ProviderSource] = []

    def register(self, provider: AuthProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"auth provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> AuthProvider | None:
        return self._providers.get(name)

    def __iter__(self) -> Iterator[AuthProvider]:
        return iter(self._providers.values())

    def add_source(self, source: ProviderSource) -> None:
        self._sources.append(source)

    async def available(self, db: AsyncSession) -> list[AuthProvider]:
        """Fixed providers followed by those of all sources (first name wins)."""
        providers = dict(self._providers)
        for source in self._sources:
            for provider in await source(db):
                providers.setdefault(provider.name, provider)
        return list(providers.values())

    async def find(self, db: AsyncSession, name: str) -> AuthProvider | None:
        if name in self._providers:
            return self._providers[name]
        for provider in await self.available(db):
            if provider.name == name:
                return provider
        return None

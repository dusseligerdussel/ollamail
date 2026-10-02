"""``PasswordAuthProvider`` for an LDAP / Active Directory directory."""

import asyncio

from app.auth.providers.base import AuthProviderKind, VerifiedIdentity
from app.auth.providers.ldap.client import LdapDirectoryClient, LdapUser
from app.auth.providers.ldap.models import PROVIDER_PREFIX
from app.auth.providers.ldap.settings import LdapDirectorySettings
from app.users.models import UserRole


def role_for_groups(settings: LdapDirectorySettings, groups: frozenset[str]) -> UserRole | None:
    """Role from the group mapping, or ``None`` if the directory does not manage roles."""
    if not settings.admin_groups:
        return None
    return UserRole.ADMIN if groups & set(settings.admin_groups) else UserRole.USER


def is_allowed(settings: LdapDirectorySettings, groups: frozenset[str]) -> bool:
    return not settings.allowed_groups or bool(groups & set(settings.allowed_groups))


class LdapAuthProvider:
    kind = AuthProviderKind.PASSWORD

    def __init__(
        self, name: str, display_name: str, settings: LdapDirectorySettings, bind_password: str
    ) -> None:
        self.name = PROVIDER_PREFIX + name
        self.display_name = display_name
        self.settings = settings
        self.client = LdapDirectoryClient(settings, bind_password)

    def identity(self, user: LdapUser) -> VerifiedIdentity:
        return VerifiedIdentity(
            provider=self.name,
            subject=user.subject,
            email=user.email,
            display_name=user.display_name,
            groups=user.groups,
        )

    async def authenticate(self, login: str, password: str) -> VerifiedIdentity | None:
        """The identity for correct credentials of an enabled, allowed account.

        Raises ``LdapUnavailableError``/``LdapConfigError`` if the directory cannot be
        used; the caller turns that into 503 instead of "wrong password".
        """
        # ldap3 blocks; the default executor bounds the number of parallel logins.
        user = await asyncio.to_thread(self.client.authenticate, login, password)
        if user is None or not is_allowed(self.settings, user.groups):
            return None
        return self.identity(user)

    def role(self, identity: VerifiedIdentity) -> UserRole | None:
        return role_for_groups(self.settings, identity.groups)

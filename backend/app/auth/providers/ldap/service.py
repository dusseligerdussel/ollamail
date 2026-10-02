"""Loading and storing directory configurations."""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Identity
from app.auth.providers.ldap.models import LdapDirectory
from app.auth.providers.ldap.provider import LdapAuthProvider
from app.auth.providers.ldap.settings import LdapDirectorySettings, TlsMode
from app.core.config import AuthSettings
from app.core.errors import ProblemError


def check_transport_security(auth: AuthSettings, settings: LdapDirectorySettings) -> None:
    """422 for unencrypted connections unless OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT is set."""
    if settings.tls_mode is TlsMode.NONE and not auth.ldap_allow_plaintext:
        raise ProblemError(
            422,
            detail=(
                "Unencrypted LDAP connections are disabled. Use LDAPS or StartTLS, or set "
                "OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT=true."
            ),
            type="urn:ollamail:problem:ldap-plaintext-disabled",
        )


async def get_directory(db: AsyncSession, name: str) -> LdapDirectory | None:
    return await db.scalar(select(LdapDirectory).where(LdapDirectory.name == name))


async def list_directories(db: AsyncSession, *, enabled_only: bool = False) -> list[LdapDirectory]:
    query = select(LdapDirectory).order_by(LdapDirectory.name)
    if enabled_only:
        query = query.where(LdapDirectory.enabled)
    return list(await db.scalars(query))


def directory_settings(directory: LdapDirectory) -> LdapDirectorySettings:
    return LdapDirectorySettings.model_validate(directory.settings)


def provider_for(directory: LdapDirectory, auth: AuthSettings) -> LdapAuthProvider:
    settings = directory_settings(directory)
    # Also checked here: the setting may have been switched off after saving.
    check_transport_security(auth, settings)
    return LdapAuthProvider(
        directory.name, directory.display_name, settings, directory.bind_password
    )


async def delete_directory(db: AsyncSession, directory: LdapDirectory) -> None:
    """Delete the directory and the identities linked through it (caller commits).

    Users stay; they can no longer sign in through this directory. Removing the links
    means a new directory with the same name cannot take over the old accounts.
    """
    await db.execute(delete(Identity).where(Identity.provider == directory.provider))
    await db.delete(directory)

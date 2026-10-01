"""Local accounts: e-mail address + Argon2id password hash in ``auth_identities``."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import LOCAL_PROVIDER, Identity
from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.auth.providers.base import AuthProviderKind, VerifiedIdentity
from app.users.models import User
from app.users.schemas import normalize_email


class LocalAuthProvider:
    name = LOCAL_PROVIDER
    display_name = "Local account"
    kind = AuthProviderKind.PASSWORD

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def authenticate(self, login: str, password: str) -> VerifiedIdentity | None:
        identity = await self._identity(login)
        if not await verify_password(identity.password_hash if identity else None, password):
            return None
        assert identity is not None and identity.password_hash is not None
        if needs_rehash(identity.password_hash):
            identity.password_hash = await hash_password(password)
        identity.last_used_at = datetime.now(UTC)
        return VerifiedIdentity(provider=self.name, subject=identity.subject)

    async def _identity(self, login: str) -> Identity | None:
        try:
            email = normalize_email(login)
        except ValueError:
            return None
        return await self._db.scalar(
            select(Identity)
            .join(User, User.id == Identity.user_id)
            .where(User.email == email, Identity.provider == LOCAL_PROVIDER)
        )

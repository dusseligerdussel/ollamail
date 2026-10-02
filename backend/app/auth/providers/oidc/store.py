"""Configured OIDC providers: environment (read-only, GitOps) plus database (admin API).

Providers are read per request instead of once at start-up, so a change in the admin API
takes effect on every API instance immediately. A database entry whose name is taken by an
environment provider is ignored.
"""

from collections.abc import Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProvider
from app.auth.providers.oidc.config import OIDCConfig, from_record, from_settings
from app.auth.providers.oidc.metadata import MetadataCache
from app.auth.providers.oidc.models import OIDCProviderRecord
from app.auth.providers.oidc.provider import OIDCProvider
from app.core.config import Settings


class OIDCProviderStore:
    def __init__(self, settings: Settings) -> None:
        auth = settings.auth
        self.allow_http = auth.oidc_allow_insecure_http
        # Tests replace this with an in-process mock IdP.
        self.transport: Callable[[], httpx.AsyncBaseTransport | None] = lambda: None
        self.metadata = MetadataCache(
            ttl=auth.oidc_metadata_cache_seconds,
            allow_http=self.allow_http,
            transport=lambda: self.transport(),
        )
        # Invalid environment configuration fails at start-up (ValueError).
        self.env_configs: dict[str, OIDCConfig] = {}
        for name, value in auth.oidc_providers.items():
            try:
                self.env_configs[name] = from_settings(name, value, allow_http=self.allow_http)
            except ValueError as exc:
                raise ValueError(f"OLLAMAIL_AUTH_OIDC_PROVIDERS[{name}]: {exc}") from None

    async def configs(self, db: AsyncSession) -> list[OIDCConfig]:
        """All providers (also disabled ones): environment first, then by name."""
        records = await db.scalars(select(OIDCProviderRecord).order_by(OIDCProviderRecord.name))
        configs = list(self.env_configs.values())
        configs += [from_record(r) for r in records if r.name not in self.env_configs]
        return configs

    async def config(self, db: AsyncSession, name: str) -> OIDCConfig | None:
        if name in self.env_configs:
            return self.env_configs[name]
        record = await db.scalar(select(OIDCProviderRecord).where(OIDCProviderRecord.name == name))
        return from_record(record) if record is not None else None

    def provider(self, config: OIDCConfig) -> OIDCProvider:
        return OIDCProvider(config, self.metadata)

    async def enabled_provider(self, db: AsyncSession, name: str) -> OIDCProvider | None:
        config = await self.config(db, name)
        if config is None or not config.enabled:
            return None
        return self.provider(config)

    async def enabled_providers(self, db: AsyncSession) -> list[AuthProvider]:
        """Source for ``AuthProviderRegistry`` (login page)."""
        return [self.provider(c) for c in await self.configs(db) if c.enabled]

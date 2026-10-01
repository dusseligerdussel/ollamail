"""OpenID Connect login (generic plus presets for Entra ID, Google, Keycloak, Authentik).

``install(app, settings)`` wires the provider store, the login-page source and the
routers into the application. See docs/auth/oidc.md.
"""

from fastapi import FastAPI

from app.auth.providers.base import AuthProviderRegistry
from app.auth.providers.oidc.config import OIDCConfig
from app.auth.providers.oidc.errors import OIDCError, OIDCErrorCode
from app.auth.providers.oidc.provider import OIDCProvider
from app.auth.providers.oidc.router import admin_router, router
from app.auth.providers.oidc.store import OIDCProviderStore
from app.core.config import Settings


def install(app: FastAPI, settings: Settings) -> OIDCProviderStore:
    store = OIDCProviderStore(settings)
    app.state.oidc = store
    registry: AuthProviderRegistry = app.state.auth_providers
    registry.add_source(store.enabled_providers)
    app.include_router(router)
    app.include_router(admin_router)
    return store


__all__ = [
    "OIDCConfig",
    "OIDCError",
    "OIDCErrorCode",
    "OIDCProvider",
    "OIDCProviderStore",
    "install",
]

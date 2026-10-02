"""SAML 2.0 login (SP-initiated) for IdPs such as Entra ID, AD FS, Okta and Keycloak.

``install(app)`` wires the login-page source and the routers into the application.
See docs/auth/saml.md.
"""

from fastapi import FastAPI

from app.auth.providers.base import AuthProviderRegistry
from app.auth.providers.saml import store
from app.auth.providers.saml.config import ACS_PATH, SAMLConfig
from app.auth.providers.saml.errors import SAMLError, SAMLErrorCode
from app.auth.providers.saml.provider import SAMLProvider
from app.auth.providers.saml.router import admin_router, router


def install(app: FastAPI) -> None:
    registry: AuthProviderRegistry = app.state.auth_providers
    registry.add_source(store.enabled_providers)
    app.include_router(router)
    app.include_router(admin_router)


__all__ = ["ACS_PATH", "SAMLConfig", "SAMLError", "SAMLErrorCode", "SAMLProvider", "install"]

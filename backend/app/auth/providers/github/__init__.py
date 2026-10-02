"""GitHub login (OAuth App or GitHub App, github.com or GitHub Enterprise Server),
optionally restricted to organizations and teams.

``install(app)`` wires the login-page source and the routers into the application.
See docs/auth/github.md.
"""

from fastapi import FastAPI

from app.auth.providers.base import AuthProviderRegistry
from app.auth.providers.github import store
from app.auth.providers.github.config import GitHubConfig
from app.auth.providers.github.errors import GitHubError, GitHubErrorCode
from app.auth.providers.github.provider import GitHubProvider
from app.auth.providers.github.router import admin_router, router


def install(app: FastAPI) -> None:
    registry: AuthProviderRegistry = app.state.auth_providers
    registry.add_source(store.enabled_providers)
    app.include_router(router)
    app.include_router(admin_router)


__all__ = ["GitHubConfig", "GitHubError", "GitHubErrorCode", "GitHubProvider", "install"]

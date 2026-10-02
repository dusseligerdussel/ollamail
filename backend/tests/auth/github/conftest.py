"""Fixtures for GitHub tests: a respx-mocked GitHub (REST API and OAuth endpoints)."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.github.models import GitHubProviderRecord
from app.core.config import AuthSettings, Settings
from app.core.crypto import KeyRing, set_keyring
from app.core.db import get_db
from app.main import create_app
from tests.conftest import api_client

PUBLIC_URL = "https://test"
CLIENT_ID = "Iv1.0123456789abcdef"
CLIENT_SECRET = "github-client-secret"
GITHUB_WEB = "https://github.com"
GITHUB_API = "https://api.github.com"
USER_ID = 4711


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"auth": AuthSettings(public_url=PUBLIC_URL)})


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)


@dataclass
class FakeGitHub:
    """GitHub as seen by the provider; change the attributes to shape the answers."""

    web: str = GITHUB_WEB
    api: str = GITHUB_API
    user: dict[str, Any] = field(
        default_factory=lambda: {"id": USER_ID, "login": "erika", "name": "Erika Mustermann"}
    )
    emails: list[dict[str, Any]] | None = field(
        default_factory=lambda: [
            {"email": "erika@example.org", "primary": True, "verified": True},
            {"email": "erika@users.noreply.github.com", "primary": False, "verified": True},
        ]
    )
    # Active org memberships (logins) and teams ("org/slug").
    orgs: set[str] = field(default_factory=lambda: {"acme"})
    pending_orgs: set[str] = field(default_factory=set)
    teams: list[str] = field(default_factory=lambda: ["acme/mail-admins", "other/devs"])
    teams_status: int = 200
    token_error: str | None = None
    token_requests: list[dict[str, str]] = field(default_factory=list)
    membership_requests: list[str] = field(default_factory=list)
    code: str = "the-code"
    access_token: str = "gho_token"

    def authorize(self, url: str) -> str:
        """The callback URL GitHub would redirect to after consent."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        return query["redirect_uri"] + "?" + urlencode({"code": self.code, "state": query["state"]})

    def _authorized(self, request: httpx.Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.access_token}"

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if self.token_error:
            return httpx.Response(200, json={"error": self.token_error})
        if form.get("code") != self.code or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(
            200, json={"access_token": self.access_token, "token_type": "bearer", "scope": ""}
        )

    def _user(self, request: httpx.Request) -> httpx.Response:
        if not self._authorized(request):
            return httpx.Response(401)
        return httpx.Response(200, json=self.user)

    def _emails(self, request: httpx.Request) -> httpx.Response:
        if self.emails is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self.emails)

    def _teams(self, request: httpx.Request) -> httpx.Response:
        if self.teams_status != 200:
            return httpx.Response(self.teams_status, json={"message": "Forbidden"})
        page = int(request.url.params.get("page", "1"))
        per_page = int(request.url.params.get("per_page", "30"))
        chunk = self.teams[(page - 1) * per_page : page * per_page]
        return httpx.Response(
            200,
            json=[
                {"slug": t.split("/")[1], "organization": {"login": t.split("/")[0]}} for t in chunk
            ],
        )

    def _membership(self, request: httpx.Request, org: str) -> httpx.Response:
        self.membership_requests.append(org)
        if org in self.orgs:
            return httpx.Response(200, json={"state": "active", "organization": {"login": org}})
        if org in self.pending_orgs:
            return httpx.Response(200, json={"state": "pending", "organization": {"login": org}})
        return httpx.Response(404, json={"message": "Not Found"})

    def install(self, router: respx.MockRouter) -> None:
        router.post(f"{self.web}/login/oauth/access_token").mock(side_effect=self._token)
        router.get(f"{self.api}/user").mock(side_effect=self._user)
        router.get(f"{self.api}/user/emails").mock(side_effect=self._emails)
        router.get(f"{self.api}/user/teams").mock(side_effect=self._teams)
        router.get(url__regex=rf"^{self.api}/user/memberships/orgs/(?P<org>[^/?]+)$").mock(
            side_effect=self._membership
        )


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    with respx.mock(assert_all_called=False) as router:
        fake.install(router)
        yield fake


@dataclass
class GitHubTestApp:
    app: FastAPI
    client: AsyncClient
    github: FakeGitHub


@pytest.fixture
async def gh(
    settings: Settings, db_session: AsyncSession, github: FakeGitHub
) -> AsyncIterator[GitHubTestApp]:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    test_app = GitHubTestApp(app=app, client=api_client(app), github=github)
    async with test_app.client:
        yield test_app
    await app.state.database.dispose()


async def add_provider(
    db: AsyncSession, name: str = "github", **overrides: Any
) -> GitHubProviderRecord:
    values: dict[str, Any] = {
        "name": name,
        "display_name": "GitHub",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "allowed_domains": [],
        "allowed_organizations": [],
        "allowed_teams": [],
    }
    values.update(overrides)
    record = GitHubProviderRecord(**values)
    db.add(record)
    await db.commit()
    return record


@dataclass
class LoginResult:
    start: httpx.Response
    callback: httpx.Response | None

    @property
    def location(self) -> str:
        response = self.callback or self.start
        return response.headers["location"]


async def sign_in(
    gh: GitHubTestApp, name: str = "github", *, return_to: str | None = None
) -> LoginResult:
    """Start, consent at (fake) GitHub, callback."""
    params = {"return_to": return_to} if return_to is not None else {}
    start = await gh.client.get(f"/auth/github/{name}/login", params=params)
    if start.status_code != 303 or start.headers["location"].startswith("/"):
        return LoginResult(start=start, callback=None)
    parts = urlsplit(gh.github.authorize(start.headers["location"]))
    assert f"{parts.scheme}://{parts.netloc}" == PUBLIC_URL
    callback = await gh.client.get(parts.path.removeprefix("/api") + "?" + parts.query)
    return LoginResult(start=start, callback=callback)

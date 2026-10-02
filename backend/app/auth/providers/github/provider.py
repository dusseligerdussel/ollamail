"""GitHub login: OAuth 2.0 Authorization Code flow with ``state`` and PKCE (S256).

Works with OAuth Apps and GitHub Apps (user access tokens) on github.com and GitHub
Enterprise Server. ``complete`` exchanges the code (back channel, client secret), then
reads from the REST API with the user's token:

* ``GET /user``: the numeric user ID (stable subject; logins can be renamed) and the name
* ``GET /user/emails``: only the *primary* address, and only if GitHub marks it verified
* ``GET /user/teams``: teams as ``<org>/<team-slug>``, the groups for the role mapping
* ``GET /user/memberships/orgs/{org}``: active membership in an allowed organization

Organization and team restrictions are checked here on the server, after the code
exchange; a user outside them gets ``not_member``. GitHub has no ID token, so the nonce
of the shared flow is not used. The access token is used only during the callback and is
never stored or logged; neither are API responses.
"""

from collections.abc import Mapping
from typing import Any

import httpx
from authlib.common.urls import add_params_to_uri
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from app.auth.providers.base import AuthProviderKind, VerifiedIdentity
from app.auth.providers.github.config import GitHubConfig
from app.auth.providers.github.errors import GitHubError, GitHubErrorCode
from app.auth.provisioning import MAX_GROUPS, ProvisioningPolicy
from app.core.logging import get_logger

log = get_logger(__name__)

# OAuth App scopes (GitHub Apps ignore them and use the app's permissions instead):
# user:email for private addresses, read:org for private org and team memberships.
SCOPES = ("read:user", "user:email", "read:org")
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_PAGE_SIZE = 100
# 100 teams per page; more than MAX_GROUPS teams are not stored anyway.
_MAX_TEAM_PAGES = MAX_GROUPS // _PAGE_SIZE


def _api_error(reason: str) -> GitHubError:
    return GitHubError(GitHubErrorCode.API_FAILED, reason)


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class GitHubProvider:
    kind = AuthProviderKind.REDIRECT

    def __init__(self, config: GitHubConfig) -> None:
        self.config = config
        self.name = config.provider_name
        self.display_name = config.display_name
        self.login_path = f"/auth/github/{config.name}/login"
        self.callback_path = f"/auth/github/{config.name}/callback"

    @property
    def provisioning(self) -> ProvisioningPolicy:
        return self.config.policy

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)

    # -- RedirectAuthProvider ---------------------------------------------------------

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, code_verifier: str
    ) -> str:
        url: str = add_params_to_uri(
            f"{self.config.web_url}/login/oauth/authorize",
            [
                ("client_id", self.config.client_id),
                ("redirect_uri", redirect_uri),
                ("scope", " ".join(SCOPES)),
                ("state", state),
                ("code_challenge", create_s256_code_challenge(code_verifier)),
                ("code_challenge_method", "S256"),
                # Accounts are created at GitHub, not during a login to this instance.
                ("allow_signup", "false"),
            ],
        )
        return url

    async def complete(
        self, *, params: Mapping[str, str], nonce: str, redirect_uri: str, code_verifier: str
    ) -> VerifiedIdentity:
        if "error" in params:
            raise GitHubError(GitHubErrorCode.IDP_ERROR)
        code = params.get("code")
        if not code:
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "missing_code")
        async with self.client() as http:
            token = await self._exchange(http, code, redirect_uri, code_verifier)
            subject, display_name = await self._user(http, token)
            teams = await self._teams(http, token)
            await self._check_membership(http, token, teams)
            email = await self._primary_email(http, token)
        return VerifiedIdentity(
            provider=self.name,
            subject=subject,
            email=email,
            display_name=display_name,
            groups=self._groups(teams),
            email_verified=email is not None,
        )

    # -- Token endpoint ---------------------------------------------------------------

    async def _exchange(
        self, http: httpx.AsyncClient, code: str, redirect_uri: str, code_verifier: str
    ) -> str:
        try:
            response = await http.post(
                f"{self.config.web_url}/login/oauth/access_token",
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError:
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "unreachable") from None
        if response.status_code != 200 or len(response.content) > _MAX_RESPONSE_BYTES:
            log.info(
                "github_token_exchange_rejected", provider=self.name, status=response.status_code
            )
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "rejected")
        try:
            body = response.json()
        except ValueError:
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "malformed") from None
        # GitHub reports a bad code, verifier or secret with status 200 and an "error" field.
        if not isinstance(body, dict) or "error" in body:
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "rejected")
        token = body.get("access_token")
        token_type = body.get("token_type")
        if not isinstance(token, str) or not token:
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "missing_token")
        if isinstance(token_type, str) and token_type.lower() != "bearer":
            raise GitHubError(GitHubErrorCode.TOKEN_EXCHANGE_FAILED, "token_type")
        return token

    # -- REST API ---------------------------------------------------------------------

    async def _get(
        self,
        http: httpx.AsyncClient,
        token: str,
        path: str,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        try:
            response = await http.get(
                self.config.api_url + path,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": "ollamail",
                },
            )
        except httpx.HTTPError:
            raise _api_error("unreachable") from None
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise _api_error("too_large")
        return response

    @staticmethod
    def _json(response: httpx.Response, step: str) -> Any:
        try:
            return response.json()
        except ValueError:
            raise _api_error(f"{step}_malformed") from None

    async def _user(self, http: httpx.AsyncClient, token: str) -> tuple[str, str | None]:
        """The user ID (subject) and display name."""
        response = await self._get(http, token, "/user")
        if response.status_code != 200:
            log.info(
                "github_api_rejected", provider=self.name, step="user", status=response.status_code
            )
            raise _api_error("user")
        data = self._json(response, "user")
        user_id = data.get("id") if isinstance(data, dict) else None
        # bool is an int subclass; the ID must be a real positive integer.
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise _api_error("user_id")
        return str(user_id), _str(data.get("name")) or _str(data.get("login"))

    async def _primary_email(self, http: httpx.AsyncClient, token: str) -> str | None:
        """The primary address if GitHub has verified it, else ``None``.

        Secondary addresses are ignored even if verified: the primary one is what the
        user chose for their account. Without access to the addresses (GitHub App without
        the "Email addresses" permission) there is none.
        """
        response = await self._get(http, token, "/user/emails", {"per_page": _PAGE_SIZE})
        if response.status_code in {403, 404}:
            log.info("github_emails_unavailable", provider=self.name, status=response.status_code)
            return None
        if response.status_code != 200:
            raise _api_error("emails")
        entries = self._json(response, "emails")
        if not isinstance(entries, list):
            raise _api_error("emails_malformed")
        for entry in entries:
            if isinstance(entry, dict) and entry.get("primary") is True:
                return _str(entry.get("email")) if entry.get("verified") is True else None
        return None

    async def _teams(self, http: httpx.AsyncClient, token: str) -> frozenset[str]:
        """The user's teams as lower-case ``<org>/<team-slug>``.

        Without permission to list them (missing ``read:org`` consent, GitHub App without
        the "Members" permission) the user has no teams: team restrictions then deny.
        """
        teams: set[str] = set()
        for page in range(1, _MAX_TEAM_PAGES + 1):
            response = await self._get(
                http, token, "/user/teams", {"per_page": _PAGE_SIZE, "page": page}
            )
            if response.status_code in {403, 404}:
                log.info(
                    "github_teams_unavailable", provider=self.name, status=response.status_code
                )
                return frozenset()
            if response.status_code != 200:
                raise _api_error("teams")
            entries = self._json(response, "teams")
            if not isinstance(entries, list):
                raise _api_error("teams_malformed")
            for entry in entries:
                team = self._team(entry)
                if team is not None:
                    teams.add(team)
            if len(entries) < _PAGE_SIZE:
                break
        return frozenset(teams)

    @staticmethod
    def _team(entry: object) -> str | None:
        if not isinstance(entry, dict):
            return None
        organization = entry.get("organization")
        org = _str(organization.get("login")) if isinstance(organization, dict) else None
        slug = _str(entry.get("slug"))
        if org is None or slug is None:
            return None
        return f"{org}/{slug}".lower()

    async def _is_member(self, http: httpx.AsyncClient, token: str, org: str) -> bool:
        response = await self._get(http, token, f"/user/memberships/orgs/{org}")
        if response.status_code in {403, 404}:
            return False
        if response.status_code != 200:
            raise _api_error("membership")
        data = self._json(response, "membership")
        # Pending invitations do not count.
        return isinstance(data, dict) and data.get("state") == "active"

    async def _check_membership(
        self, http: httpx.AsyncClient, token: str, teams: frozenset[str]
    ) -> None:
        if not self.config.restricted:
            return
        if teams & self.config.allowed_teams:
            return
        for org in sorted(self.config.allowed_organizations):
            if await self._is_member(http, token, org):
                return
        raise GitHubError(GitHubErrorCode.NOT_MEMBER)

    def _groups(self, teams: frozenset[str]) -> frozenset[str]:
        """Teams for the role mapping; with a restriction only those of the allowed
        organizations (teams elsewhere are none of this instance's business)."""
        if not self.config.restricted:
            return teams
        orgs = self.config.allowed_organizations | {
            t.partition("/")[0] for t in self.config.allowed_teams
        }
        return frozenset(t for t in teams if t.partition("/")[0] in orgs)

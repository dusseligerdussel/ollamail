"""End-to-end login flow against the mock IdP: PKCE, state, nonce, sessions, logout."""

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import AuthSession, Identity
from app.auth.redirect_flow import FLOW_COOKIE
from app.auth.sessions import SESSION_COOKIE
from app.core.config import AuthSettings, OIDCProviderSettings, Settings
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import make_local_user
from tests.auth.oidc.conftest import PUBLIC_URL, OIDCTestApp, add_provider, make_app, sign_in
from tests.auth.oidc.mock_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, MockIdP

pytestmark = pytest.mark.db


async def test_providers_endpoint_lists_enabled_oidc_providers(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, "corp", display_name="Corp SSO")
    await add_provider(db_session, "off", enabled=False)

    providers = (await oidc.client.get("/auth/providers")).json()["providers"]

    assert providers == [
        {
            "name": "oidc:corp",
            "display_name": "Corp SSO",
            "kind": "redirect",
            "login_path": "/auth/oidc/corp/login",
        }
    ]


async def test_login_redirects_with_pkce_state_and_nonce(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)

    response = await oidc.client.get("/auth/oidc/test/login")

    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{oidc.idp.issuer}/authorize"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC_URL}/api/auth/oidc/test/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 43
    assert len(query["nonce"]) >= 43
    cookie = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith(f"{FLOW_COOKIE}=")
    ).lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    # The cookie is encrypted: neither state nor verifier are readable.
    assert query["state"] not in cookie


async def test_first_login_provisions_user_and_starts_session(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)

    result = await sign_in(oidc, return_to="/inbox?filter=todo")

    assert result.callback is not None
    assert result.callback.status_code == 303
    assert result.location == "/inbox?filter=todo"
    assert SESSION_COOKIE in oidc.client.cookies
    assert FLOW_COOKIE not in oidc.client.cookies
    me = (await oidc.client.get("/auth/me")).json()
    assert me["email"] == "erika@example.org"
    assert me["display_name"] == "Erika Mustermann"
    assert me["role"] == UserRole.USER
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "oidc:test"))
    assert identity is not None
    assert identity.subject == "user-123"
    assert identity.groups == ["mail-admins", "staff"]
    session = await db_session.scalar(select(AuthSession))
    assert session is not None and session.provider == "oidc:test"
    (login,) = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert login.details == {"provider": "oidc:test"}
    (created,) = await audit_rows(db_session, AuditAction.USER_CREATED)
    assert created.details == {"role": "user", "provider": "oidc:test"}
    # PKCE: the token request carried the verifier, authenticated with client_secret_basic.
    token_request = oidc.idp.token_requests[-1]
    assert token_request["code_verifier"]
    assert "client_secret" not in token_request


async def test_second_login_reuses_user_and_updates_groups(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await sign_in(oidc)
    oidc.idp.claims = {"groups": ["staff"], "email": "renamed@example.org"}

    result = await sign_in(oidc)

    assert result.location == "/"
    users = (await db_session.scalars(select(User))).all()
    assert len(users) == 1
    # The address is not changed by the IdP (it could collide with another user).
    assert users[0].email == "erika@example.org"
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "oidc:test"))
    assert identity is not None and identity.groups == ["staff"]


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\evil", "x"])
async def test_return_to_cannot_leave_the_site(
    oidc: OIDCTestApp, db_session: AsyncSession, target: str
) -> None:
    await add_provider(db_session)

    result = await sign_in(oidc, return_to=target)

    assert result.location == "/"


async def test_public_client_without_secret_uses_pkce_only(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.idp.client_secret = None
    await add_provider(db_session, client_secret=None)

    result = await sign_in(oidc)

    assert result.location == "/"
    assert oidc.idp.token_requests[-1]["client_id"] == CLIENT_ID


async def test_client_secret_post_when_basic_is_not_supported(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.idp.token_endpoint_auth_methods = ["client_secret_post"]
    await add_provider(db_session)

    result = await sign_in(oidc)

    assert result.location == "/"
    assert oidc.idp.token_requests[-1]["client_secret"] == CLIENT_SECRET


async def test_missing_email_is_taken_from_userinfo(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.idp.claims = {"email": None, "email_verified": None, "groups": None}
    oidc.idp.userinfo = {
        "sub": "user-123",
        "email": "erika@example.org",
        "email_verified": True,
        "groups": ["staff"],
    }
    await add_provider(db_session)

    result = await sign_in(oidc)

    assert result.location == "/"
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "oidc:test"))
    assert identity is not None and identity.groups == ["staff"]


async def test_userinfo_of_another_subject_is_ignored(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.idp.claims = {"email": None}
    oidc.idp.userinfo = {"sub": "someone-else", "email": "victim@example.org"}
    await add_provider(db_session)

    result = await sign_in(oidc)

    assert result.location == "/login?error=email_missing"
    (failed,) = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert failed.details == {"provider": "oidc:test", "reason": "email_missing"}


async def test_disabled_or_unknown_provider_cannot_start_a_login(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, enabled=False)

    disabled = await oidc.client.get("/auth/oidc/test/login")
    unknown = await oidc.client.get("/auth/oidc/nope/login")

    assert disabled.headers["location"] == "/login?error=provider_unknown"
    assert unknown.headers["location"] == "/login?error=provider_unknown"


async def test_unreachable_idp_redirects_with_error(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, issuer="https://idp.test/realms/missing")

    response = await oidc.client.get("/auth/oidc/test/login")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=provider_unavailable"


async def test_idp_error_response_is_reported(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)

    def deny(url: str) -> str:
        state = parse_qs(urlsplit(url).query)["state"][0]
        return url.split("?")[0] + f"?error=access_denied&state={state}"

    result = await sign_in(oidc, tamper=deny)

    assert result.location == "/login?error=idp_error"
    assert SESSION_COOKIE not in oidc.client.cookies


async def test_rp_initiated_logout_returns_end_session_url(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await sign_in(oidc)

    response = await oidc.client.post("/auth/oidc/logout")

    assert response.status_code == 200
    url = urlsplit(response.json()["redirect_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{oidc.idp.issuer}/logout"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query == {"client_id": CLIENT_ID, "post_logout_redirect_uri": f"{PUBLIC_URL}/login"}
    assert SESSION_COOKIE not in oidc.client.cookies
    assert (await oidc.client.get("/auth/me")).status_code == 401


async def test_logout_of_local_session_has_no_redirect(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await make_local_user(db_session)
    await oidc.client.post(
        "/auth/login",
        json={"email": "erika@example.org", "password": "correct horse battery staple"},
    )

    response = await oidc.client.post("/auth/oidc/logout")

    assert response.json() == {"redirect_url": None}
    assert (await oidc.client.get("/auth/me")).status_code == 401


@pytest.fixture
async def env_oidc(
    settings: Settings, db_session: AsyncSession, idp: MockIdP
) -> AsyncIterator[OIDCTestApp]:
    auth = AuthSettings(
        public_url=PUBLIC_URL,
        oidc_providers={
            "gitops": OIDCProviderSettings(
                display_name="GitOps IdP",
                preset="keycloak",
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret=SecretStr(CLIENT_SECRET),
            )
        },
    )
    test_app = await make_app(settings.model_copy(update={"auth": auth}), db_session, idp)
    async with test_app.client:
        yield test_app
    await test_app.app.state.database.dispose()


async def test_env_provider_signs_in(env_oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    providers = (await env_oidc.client.get("/auth/providers")).json()["providers"]
    assert [p["name"] for p in providers] == ["oidc:gitops"]

    result = await sign_in(env_oidc, "gitops")

    assert result.location == "/"
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.provider == "oidc:gitops"

"""Login flow against a mocked GitHub: PKCE, state, verified e-mail, org and team checks."""

from urllib.parse import parse_qs, urlsplit

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import AuthSession, Identity
from app.auth.redirect_flow import FLOW_COOKIE
from app.auth.sessions import SESSION_COOKIE
from app.users.models import User
from tests.audit.conftest import audit_rows
from tests.auth.conftest import make_local_user
from tests.auth.github.conftest import (
    CLIENT_ID,
    PUBLIC_URL,
    USER_ID,
    FakeGitHub,
    GitHubTestApp,
    add_provider,
    sign_in,
)

pytestmark = pytest.mark.db


async def _users(db: AsyncSession) -> list[User]:
    return list((await db.scalars(select(User))).all())


async def test_providers_endpoint_lists_enabled_github_providers(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await add_provider(db_session, "off", enabled=False)

    providers = (await gh.client.get("/auth/providers")).json()["providers"]

    assert providers == [
        {
            "name": "github:github",
            "display_name": "GitHub",
            "kind": "redirect",
            "login_path": "/auth/github/github/login",
        }
    ]


async def test_login_redirects_to_github_with_state_and_pkce(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)

    response = await gh.client.get("/auth/github/github/login")

    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/login/oauth/authorize"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == f"{PUBLIC_URL}/api/auth/github/github/callback"
    assert query["scope"] == "read:user user:email read:org"
    assert query["code_challenge_method"] == "S256"
    assert len(query["code_challenge"]) == 43
    assert len(query["state"]) >= 43
    assert query["allow_signup"] == "false"
    assert any(c.startswith(f"{FLOW_COOKIE}=") for c in response.headers.get_list("set-cookie"))


async def test_org_member_signs_in_and_is_provisioned(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, allowed_organizations=["acme"])

    result = await sign_in(gh, return_to="/inbox")

    assert result.callback is not None
    assert result.location == "/inbox"
    assert SESSION_COOKIE in gh.client.cookies
    me = (await gh.client.get("/auth/me")).json()
    assert me["email"] == "erika@example.org"
    assert me["display_name"] == "Erika Mustermann"
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "github:github"))
    assert identity is not None
    # The numeric user ID, not the (renamable) login.
    assert identity.subject == str(USER_ID)
    # Only teams of the allowed organizations are kept as groups.
    assert identity.groups == ["acme/mail-admins"]
    session = await db_session.scalar(select(AuthSession))
    assert session is not None and session.provider == "github:github"
    assert gh.github.membership_requests == ["acme"]
    # PKCE: the code exchange carried the verifier.
    assert gh.github.token_requests[-1]["code_verifier"]
    (login,) = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert login.details == {"provider": "github:github"}
    (created,) = await audit_rows(db_session, AuditAction.USER_CREATED)
    assert created.details == {"role": "user", "provider": "github:github"}


async def test_non_member_is_rejected(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, allowed_organizations=["acme"])
    gh.github.orgs = set()
    gh.github.teams = ["other/devs"]

    result = await sign_in(gh)

    assert result.location == "/login?error=not_member"
    assert SESSION_COOKIE not in gh.client.cookies
    assert await _users(db_session) == []
    (failed,) = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert failed.details == {"provider": "github:github", "reason": "not_member"}


async def test_pending_invitation_is_not_membership(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, allowed_organizations=["acme"])
    gh.github.orgs = set()
    gh.github.pending_orgs = {"acme"}
    gh.github.teams = []

    result = await sign_in(gh)

    assert result.location == "/login?error=not_member"


async def test_team_member_signs_in_without_org_check(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, allowed_teams=["acme/mail-admins"])

    result = await sign_in(gh)

    assert result.location == "/"
    assert gh.github.membership_requests == []
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.groups == ["acme/mail-admins"]


async def test_other_team_of_the_org_is_rejected(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, allowed_teams=["acme/mail-admins"])
    gh.github.teams = ["acme/sales"]

    result = await sign_in(gh)

    assert result.location == "/login?error=not_member"


async def test_team_restriction_fails_closed_without_team_access(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    # E.g. a GitHub App without the "Members" permission: teams cannot be read.
    await add_provider(db_session, allowed_teams=["acme/mail-admins"])
    gh.github.teams_status = 403

    result = await sign_in(gh)

    assert result.location == "/login?error=not_member"


async def test_without_restriction_all_teams_are_groups(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    gh.github.orgs = set()

    result = await sign_in(gh)

    assert result.location == "/"
    assert gh.github.membership_requests == []
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.groups == ["acme/mail-admins", "other/devs"]


async def test_teams_are_read_from_all_pages(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)
    gh.github.teams = [f"acme/team-{i:03}" for i in range(150)]

    await sign_in(gh)

    identity = await db_session.scalar(select(Identity))
    assert identity is not None and len(identity.groups) == 150


async def test_unverified_primary_email_is_not_used(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, allowed_organizations=["acme"])
    gh.github.emails = [
        {"email": "erika@example.org", "primary": True, "verified": False},
        # A verified secondary address is not a substitute for the primary one.
        {"email": "erika@example.net", "primary": False, "verified": True},
    ]

    result = await sign_in(gh)

    assert result.location == "/login?error=email_missing"
    assert await _users(db_session) == []
    (failed,) = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert failed.details == {"provider": "github:github", "reason": "email_missing"}


async def test_no_access_to_emails_means_no_email(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    gh.github.emails = None

    result = await sign_in(gh)

    assert result.location == "/login?error=email_missing"


async def test_known_identity_signs_in_again_and_updates_groups(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await sign_in(gh)
    gh.client.cookies.clear()
    gh.github.teams = ["acme/sales"]
    gh.github.user = {"id": USER_ID, "login": "erika-renamed", "name": None}

    result = await sign_in(gh)

    assert result.location == "/"
    assert len(await _users(db_session)) == 1
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.groups == ["acme/sales"]


async def test_existing_account_is_linked_only_if_allowed(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    user = await make_local_user(db_session, "erika@example.org")
    record = await add_provider(db_session)

    refused = await sign_in(gh)
    assert refused.location == "/login?error=email_conflict"

    record.link_by_email = True
    await db_session.commit()
    gh.client.cookies.clear()
    linked = await sign_in(gh)

    assert linked.location == "/"
    identity = await db_session.scalar(select(Identity).where(Identity.provider == "github:github"))
    assert identity is not None and identity.user_id == user.id


async def test_domain_allowlist(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, allowed_domains=["example.com"])

    result = await sign_in(gh)

    assert result.location == "/login?error=domain_not_allowed"


async def test_user_cancelled_at_github(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)
    start = await gh.client.get("/auth/github/github/login")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

    callback = await gh.client.get(
        "/auth/github/github/callback", params={"error": "access_denied", "state": state}
    )

    assert callback.headers["location"] == "/login?error=idp_error"
    assert gh.github.token_requests == []


async def test_rejected_code_exchange(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)
    gh.github.token_error = "bad_verification_code"

    result = await sign_in(gh)

    assert result.location == "/login?error=token_exchange_failed"


async def test_wrong_state_is_rejected_before_github_is_called(
    gh: GitHubTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await gh.client.get("/auth/github/github/login")

    callback = await gh.client.get(
        "/auth/github/github/callback", params={"code": "the-code", "state": "forged"}
    )

    assert callback.headers["location"] == "/login?error=state_invalid"
    assert gh.github.token_requests == []


async def test_unknown_or_disabled_provider(gh: GitHubTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, "off", enabled=False)

    for name in ("missing", "off"):
        response = await gh.client.get(f"/auth/github/{name}/login")
        assert response.headers["location"] == "/login?error=provider_unknown"


async def test_github_enterprise_server(db_session: AsyncSession, gh: GitHubTestApp) -> None:
    ghes = FakeGitHub(web="https://github.example.org", api="https://github.example.org/api/v3")
    await add_provider(db_session, "corp", base_url="https://github.example.org")
    with respx.mock(assert_all_called=False) as router:
        ghes.install(router)
        gh.github = ghes
        start = await gh.client.get("/auth/github/corp/login")
        assert start.headers["location"].startswith(
            "https://github.example.org/login/oauth/authorize?"
        )
        result = await sign_in(gh, "corp")

    assert result.location == "/"
    assert ghes.token_requests
    identity = await db_session.scalar(select(Identity))
    assert identity is not None and identity.provider == "github:corp"

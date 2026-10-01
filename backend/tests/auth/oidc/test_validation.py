"""Rejected logins: state, nonce, PKCE, signature, issuer, audience, expiry, presets."""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from joserfc.jwk import RSAKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.auth.providers.oidc.presets import OIDCPreset
from app.auth.redirect_flow import FLOW_COOKIE
from app.auth.sessions import SESSION_COOKIE
from tests.auth.oidc.conftest import OIDCTestApp, add_provider, sign_in
from tests.auth.oidc.mock_idp import CLIENT_ID, CLIENT_SECRET, MockIdP, hmac_key

pytestmark = pytest.mark.db


async def _no_session(oidc: OIDCTestApp, db: AsyncSession) -> bool:
    count = await db.scalar(select(func.count()).select_from(AuthSession))
    return count == 0 and SESSION_COOKIE not in oidc.client.cookies


def _with_query(url: str, **changes: str) -> str:
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    query.update(changes)
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query)}"


# -- state / flow cookie ------------------------------------------------------------------


async def test_wrong_state_is_rejected(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)

    result = await sign_in(oidc, tamper=lambda url: _with_query(url, state="forged"))

    assert result.location == "/login?error=state_invalid"
    assert await _no_session(oidc, db_session)
    assert oidc.idp.token_requests == []


async def test_callback_without_flow_cookie_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    """Login CSRF: a callback URL planted by an attacker carries no matching cookie."""
    await add_provider(db_session)

    def drop_cookie(url: str) -> str:
        oidc.client.cookies.delete(FLOW_COOKIE)
        return url

    result = await sign_in(oidc, tamper=drop_cookie)

    assert result.location == "/login?error=state_invalid"
    assert await _no_session(oidc, db_session)


async def test_tampered_flow_cookie_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)

    def tamper_cookie(url: str) -> str:
        value = oidc.client.cookies[FLOW_COOKIE]
        oidc.client.cookies.set(
            FLOW_COOKIE, value[:-4] + ("AAAA" if value[-4:] != "AAAA" else "BBBB")
        )
        return url

    result = await sign_in(oidc, tamper=tamper_cookie)

    assert result.location == "/login?error=state_invalid"


async def test_flow_of_another_provider_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await add_provider(db_session, "other")

    result = await sign_in(
        oidc, tamper=lambda url: url.replace("/oidc/test/callback", "/oidc/other/callback")
    )

    assert result.location == "/login?error=state_invalid"


async def test_expired_flow_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_provider(db_session)

    def later(url: str) -> str:
        now = time.time()
        monkeypatch.setattr("app.auth.redirect_flow.time.time", lambda: now + 3600)
        return url

    result = await sign_in(oidc, tamper=later)

    assert result.location == "/login?error=state_invalid"


async def test_callback_cannot_be_replayed(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session)
    captured: list[str] = []

    def capture(url: str) -> str:
        captured.append(url)
        return url

    first = await sign_in(oidc, tamper=capture)
    assert first.location == "/"
    replay = await oidc.client.get(captured[0].removeprefix("https://test/api"))

    assert replay.headers["location"] == "/login?error=state_invalid"


async def test_code_from_another_flow_fails_pkce(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    """An authorization code injected into another browser's flow cannot be redeemed: the
    code is bound to the PKCE challenge of the flow it was issued for."""
    await add_provider(db_session)

    def code_of_other_flow(url: str) -> str:
        code = parse_qs(urlsplit(url).query)["code"][0]
        oidc.idp.grants[code].code_challenge = "challenge-of-the-victims-flow-0123456789abc"
        return url

    result = await sign_in(oidc, tamper=code_of_other_flow)

    assert result.location == "/login?error=token_exchange_failed"
    assert await _no_session(oidc, db_session)


# -- ID token -----------------------------------------------------------------------------


def _claims(**claims: Any) -> Callable[[MockIdP], None]:
    def apply(idp: MockIdP) -> None:
        idp.claims = claims

    return apply


def _other_key(kid: str) -> Callable[[MockIdP], None]:
    def apply(idp: MockIdP) -> None:
        idp.signing_key = RSAKey.generate_key(2048, parameters={"kid": kid})

    return apply


def _alg_none(idp: MockIdP) -> None:
    idp.alg = "none"


def _hs256_with_client_secret(idp: MockIdP) -> None:
    idp.alg = "HS256"
    idp.signing_key = hmac_key(CLIENT_SECRET)


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param(_claims(nonce="replayed-nonce"), id="wrong-nonce"),
        pytest.param(_claims(nonce=None), id="missing-nonce"),
        pytest.param(_other_key("k1"), id="bad-signature"),
        pytest.param(_other_key("k2"), id="unknown-key"),
        pytest.param(_alg_none, id="alg-none"),
        pytest.param(_hs256_with_client_secret, id="hs256-client-secret"),
        pytest.param(_claims(iss="https://evil.example/realms/test"), id="wrong-issuer"),
        pytest.param(_claims(iss=None), id="missing-issuer"),
        pytest.param(_claims(aud="another-client"), id="wrong-audience"),
        pytest.param(_claims(aud=["another-client"]), id="wrong-audience-list"),
        pytest.param(_claims(aud=[CLIENT_ID, "another-client"]), id="multi-audience-no-azp"),
        pytest.param(
            _claims(aud=[CLIENT_ID, "another-client"], azp="another-client"), id="wrong-azp"
        ),
        pytest.param(_claims(exp=int(time.time()) - 600), id="expired"),
        pytest.param(_claims(nbf=int(time.time()) + 600), id="not-yet-valid"),
        pytest.param(_claims(iat=int(time.time()) + 600), id="issued-in-future"),
        pytest.param(_claims(sub=None), id="missing-subject"),
    ],
)
async def test_invalid_id_token_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession, tamper: Callable[[MockIdP], None]
) -> None:
    await add_provider(db_session)
    tamper(oidc.idp)

    result = await sign_in(oidc)

    assert result.location == "/login?error=invalid_token"
    assert await _no_session(oidc, db_session)


async def test_multiple_audiences_with_matching_azp_are_accepted(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    oidc.idp.claims = {"aud": [CLIENT_ID, "api"], "azp": CLIENT_ID}

    assert (await sign_in(oidc)).location == "/"


async def test_unknown_key_id_refreshes_keys_once(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    """Key rotation: a new key ID triggers one JWKS refresh; repeated unknown key IDs do
    not hammer the IdP."""
    await add_provider(db_session)
    assert (await sign_in(oidc)).location == "/"
    assert oidc.idp.jwks_requests == 1

    oidc.idp.key = RSAKey.generate_key(2048, parameters={"kid": "k2"})
    assert (await sign_in(oidc)).location == "/"
    assert oidc.idp.jwks_requests == 2

    oidc.idp.signing_key = RSAKey.generate_key(2048, parameters={"kid": "k3"})
    assert (await sign_in(oidc)).location == "/login?error=invalid_token"
    assert oidc.idp.jwks_requests == 2


async def test_discovery_with_foreign_issuer_is_rejected(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    oidc.idp.discovery_issuer = "https://evil.example/realms/test"

    response = await oidc.client.get("/auth/oidc/test/login")

    assert response.headers["location"] == "/login?error=provider_unavailable"


# -- Microsoft Entra ID -------------------------------------------------------------------

TENANT = "11111111-1111-4111-8111-111111111111"
OTHER_TENANT = "22222222-2222-4222-8222-222222222222"


def _entra_issuer(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/v2.0"


async def test_entra_single_tenant(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    oidc.use_idp(MockIdP(issuer=_entra_issuer(TENANT), claims={"tid": TENANT}))
    await add_provider(db_session, preset=OIDCPreset.ENTRA, issuer=_entra_issuer(TENANT))

    assert (await sign_in(oidc)).location == "/"


async def test_entra_single_tenant_rejects_other_tid(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.use_idp(MockIdP(issuer=_entra_issuer(TENANT), claims={"tid": OTHER_TENANT}))
    await add_provider(db_session, preset=OIDCPreset.ENTRA, issuer=_entra_issuer(TENANT))

    assert (await sign_in(oidc)).location == "/login?error=invalid_token"


def _multi_tenant_idp(tid: str, *, iss_tenant: str | None = None) -> MockIdP:
    return MockIdP(
        issuer=_entra_issuer(iss_tenant or tid),
        base=_entra_issuer("organizations"),
        discovery_issuer=_entra_issuer("{tenantid}"),
        claims={"tid": tid},
    )


@pytest.mark.parametrize(
    ("idp", "expected"),
    [
        pytest.param(_multi_tenant_idp(TENANT), "/", id="allowed-tenant"),
        pytest.param(
            _multi_tenant_idp(OTHER_TENANT), "/login?error=invalid_token", id="other-tenant"
        ),
        pytest.param(
            _multi_tenant_idp(TENANT, iss_tenant=OTHER_TENANT),
            "/login?error=invalid_token",
            id="issuer-tid-mismatch",
        ),
    ],
)
async def test_entra_multi_tenant_checks_tid(
    oidc: OIDCTestApp, db_session: AsyncSession, idp: MockIdP, expected: str
) -> None:
    oidc.use_idp(idp)
    await add_provider(
        db_session,
        preset=OIDCPreset.ENTRA,
        issuer=_entra_issuer("organizations"),
        allowed_tenants=[TENANT],
    )

    assert (await sign_in(oidc)).location == expected


async def test_entra_email_is_unverified_without_xms_edov(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    oidc.use_idp(
        MockIdP(issuer=_entra_issuer(TENANT), claims={"tid": TENANT, "email_verified": None})
    )
    await add_provider(
        db_session,
        preset=OIDCPreset.ENTRA,
        issuer=_entra_issuer(TENANT),
        allowed_domains=["example.org"],
    )

    assert (await sign_in(oidc)).location == "/login?error=domain_not_allowed"

    oidc.idp.claims = {"tid": TENANT, "email_verified": None, "xms_edov": True}
    assert (await sign_in(oidc)).location == "/"


# -- Google Workspace ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hd", "expected"),
    [
        ("example.org", "/"),
        ("evil.example", "/login?error=invalid_token"),
        (None, "/login?error=invalid_token"),
    ],
)
async def test_google_hosted_domain(
    oidc: OIDCTestApp, db_session: AsyncSession, hd: str | None, expected: str
) -> None:
    oidc.use_idp(MockIdP(issuer="https://accounts.google.com", claims={"hd": hd}))
    await add_provider(
        db_session,
        preset=OIDCPreset.GOOGLE,
        issuer="https://accounts.google.com",
        hosted_domains=["example.org"],
        groups_claim=None,
    )

    result = await sign_in(oidc)

    assert parse_qs(urlsplit(result.start.headers["location"]).query)["hd"] == ["example.org"]
    assert result.location == expected

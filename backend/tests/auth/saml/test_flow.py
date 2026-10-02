"""SAML login against the test IdP: happy path, provisioning, role mapping and every
check the response must pass (signature, time window, audience, destination,
InResponseTo, replay, XXE)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from onelogin.saml2.constants import OneLogin_Saml2_Constants as Constants
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth import redirect_flow
from app.auth.models import AuthPolicy, AuthSession, Identity, RoleMappingRule
from app.auth.providers.saml.presets import NAMEID_EMAIL, NAMEID_TRANSIENT
from app.auth.redirect_flow import FLOW_COOKIE
from app.auth.sessions import SESSION_COOKIE
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.saml.conftest import (
    ACS_URL,
    DEFAULT_ATTRIBUTES,
    SAMLTestApp,
    add_provider,
    error_of,
    post_acs,
    sign_in,
    sp_entity_id,
    start,
)
from tests.auth.saml.idp import IDP_SSO_URL, make_key_pair

pytestmark = pytest.mark.db


async def _users(db: AsyncSession) -> list[User]:
    return list((await db.scalars(select(User))).all())


async def test_providers_endpoint_lists_enabled_saml_providers(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)
    await add_provider(db_session, saml.idp, "off", enabled=False)

    providers = (await saml.client.get("/auth/providers")).json()["providers"]

    assert providers == [
        {
            "name": "saml:corp",
            "display_name": "Corp SSO",
            "kind": "redirect",
            "login_path": "/auth/saml/corp/login",
        }
    ]


async def test_login_redirects_with_authn_request(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)

    started = await start(saml)

    assert started.response.headers["location"].startswith(IDP_SSO_URL + "?SAMLRequest=")
    xml = started.authn_request
    assert f'AssertionConsumerServiceURL="{ACS_URL}"' in xml
    assert f'Destination="{IDP_SSO_URL}"' in xml
    assert f">{sp_entity_id()}</saml:Issuer>" in xml
    assert 'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"' in xml
    # No authentication context: the IdP may use Kerberos, MFA, ...
    assert "RequestedAuthnContext" not in xml
    assert started.request_id.startswith("_")
    assert len(started.relay_state) >= 43
    cookie = next(
        c for c in started.response.headers.get_list("set-cookie") if c.startswith(FLOW_COOKIE)
    )
    # The IdP posts back cross-site: the flow cookie must be SameSite=None (and Secure).
    assert "samesite=none" in cookie.lower()
    assert "secure" in cookie.lower()


async def test_login_provisions_user_and_starts_session(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)

    response = await sign_in(saml, return_to="/inbox")

    assert response.status_code == 303
    assert response.headers["location"] == "/inbox"
    assert any(c.startswith(f"{SESSION_COOKIE}=") for c in response.headers.get_list("set-cookie"))
    (user,) = await _users(db_session)
    assert user.email == "erika@example.org"
    assert user.display_name == "Erika Mustermann"
    assert user.role is UserRole.USER
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None
    assert (identity.provider, identity.subject) == ("saml:corp", "u-4711")
    assert sorted(identity.groups) == ["mail-admins", "staff"]
    session = await db_session.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
    assert session is not None and session.provider == "saml:corp"
    assert (await saml.client.get("/auth/me")).json()["email"] == "erika@example.org"
    (succeeded,) = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert succeeded.details == {"provider": "saml:corp"}


async def test_second_login_finds_the_same_user(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)

    assert error_of(await sign_in(saml)) is None
    saml.client.cookies.clear()
    assert error_of(await sign_in(saml, attributes={"groups": ["staff"]})) is None

    (user,) = await _users(db_session)
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user.id))
    assert identity is not None and identity.groups == ["staff"]


async def test_response_signature_only_is_accepted(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    """Keycloak's default: the whole response is signed, the assertion is not."""
    await add_provider(db_session, saml.idp)

    response = await sign_in(saml, sign_assertion=False, sign_response=True)

    assert error_of(response) is None


async def test_certificate_rollover_accepts_both_certificates(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    new_keys = make_key_pair("New IdP certificate")
    await add_provider(
        db_session, saml.idp, idp_certificates=[saml.idp.keys.cert_b64, new_keys.cert_b64]
    )

    assert error_of(await sign_in(saml, signing_keys=new_keys)) is None


async def test_role_mapping_uses_saml_groups(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, saml.idp)
    db_session.add(AuthPolicy(role_mapping_enabled=True, default_role=UserRole.USER))
    db_session.add(RoleMappingRule(group="Mail-Admins", provider="saml:corp", role=UserRole.ADMIN))
    await db_session.commit()

    assert error_of(await sign_in(saml)) is None

    (user,) = await _users(db_session)
    assert user.role is UserRole.ADMIN


async def test_subject_attribute_and_email_name_id(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    """Entra ID: the object ID is the subject; the e-mail can come from the NameID."""
    oid = "http://schemas.microsoft.com/identity/claims/objectidentifier"
    await add_provider(db_session, saml.idp, subject_attribute=oid, email_attribute=None)

    response = await sign_in(
        saml,
        name_id="erika@example.org",
        name_id_format=NAMEID_EMAIL,
        attributes={oid: ["8c2a6e0c-1111-2222-3333-444455556666"]},
    )

    assert error_of(response) is None
    identity = await db_session.scalar(select(Identity))
    assert identity is not None
    assert identity.subject == "8c2a6e0c-1111-2222-3333-444455556666"
    (user,) = await _users(db_session)
    assert user.email == "erika@example.org"


async def test_untrusted_email_does_not_pass_domain_allowlist(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp, trust_email=False, allowed_domains=["example.org"])

    assert error_of(await sign_in(saml)) == "domain_not_allowed"


async def test_disabled_provider_cannot_start_or_finish(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    record = await add_provider(db_session, saml.idp)
    started = await start(saml)
    record.enabled = False
    await db_session.commit()

    blocked = await saml.client.get("/auth/saml/corp/login")
    finished = await post_acs(
        saml,
        saml.idp.response(
            in_response_to=started.request_id, acs_url=ACS_URL, audience=sp_entity_id()
        ),
        started.relay_state,
    )

    assert error_of(blocked) == "provider_unknown"
    assert error_of(finished) == "provider_unknown"
    assert await _users(db_session) == []


# -- Rejected responses ------------------------------------------------------------------


async def _rejected(saml: SAMLTestApp, db: AsyncSession, **response_args: object) -> str | None:
    response = await sign_in(saml, **response_args)  # type: ignore[arg-type]
    assert response.status_code == 303
    assert not any(
        c.startswith(f"{SESSION_COOKIE}=") for c in response.headers.get_list("set-cookie")
    )
    assert await _users(db) == []
    return error_of(response)


@pytest.mark.parametrize(
    ("response_args", "check"),
    [
        pytest.param(
            {"signing_keys": make_key_pair("Attacker")}, "invalid_signature", id="wrong-signature"
        ),
        pytest.param(
            {"tamper": ("erika@example.org", "admin@example.org")},
            "invalid_signature",
            id="tampered-after-signing",
        ),
        pytest.param({"sign_assertion": False}, "no_signature_found", id="unsigned"),
        pytest.param(
            {"sign_algorithm": Constants.RSA_SHA1, "digest_algorithm": Constants.SHA1},
            "deprecated_signature_method",
            id="sha1-signature",
        ),
        pytest.param(
            {
                "issued_at": datetime.now(UTC) - timedelta(minutes=30),
                "not_on_or_after": datetime.now(UTC) - timedelta(minutes=20),
            },
            "assertion_expired",
            id="expired",
        ),
        pytest.param(
            {"not_before": datetime.now(UTC) + timedelta(minutes=20)},
            "assertion_too_early",
            id="not-yet-valid",
        ),
        pytest.param(
            {"audience": "https://other.example.org/sp"}, "wrong_audience", id="wrong-audience"
        ),
        pytest.param(
            {"destination": "https://other.example.org/acs"},
            "wrong_destination",
            id="wrong-destination",
        ),
        pytest.param({"destination": None}, "wrong_destination", id="missing-destination"),
        pytest.param(
            {"recipient": "https://other.example.org/acs"},
            "wrong_subjectconfirmation",
            id="wrong-recipient",
        ),
        pytest.param({"recipient": None}, "wrong_subjectconfirmation", id="missing-recipient"),
        pytest.param({"in_response_to": "_other"}, "wrong_inresponseto", id="wrong-inresponseto"),
        pytest.param(
            {"in_response_to": None, "assertion_in_response_to": None},
            "wrong_inresponseto",
            id="unsolicited",
        ),
        pytest.param(
            {"assertion_in_response_to": "_other"},
            "wrong_subjectconfirmation",
            id="assertion-for-another-request",
        ),
        pytest.param({"issuer": "https://evil.example.org"}, "wrong_issuer", id="wrong-issuer"),
        pytest.param({"name_id": None}, "missing_subject", id="missing-subject"),
        pytest.param(
            {"name_id": "short-lived", "name_id_format": NAMEID_TRANSIENT},
            "transient_name_id",
            id="transient-name-id",
        ),
    ],
)
async def test_invalid_responses_are_rejected(
    saml: SAMLTestApp,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    response_args: dict[str, object],
    check: str,
) -> None:
    await add_provider(db_session, saml.idp)
    if "audience" not in response_args:
        response_args = {"audience": sp_entity_id(), **response_args}

    started = await start(saml)
    args = {"in_response_to": started.request_id, "acs_url": ACS_URL, **response_args}
    args.setdefault("attributes", DEFAULT_ATTRIBUTES)
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        redirect_flow.log, "warning", lambda event, **kw: logged.append({"event": event, **kw})
    )
    response = await post_acs(saml, saml.idp.response(**args), started.relay_state)  # type: ignore[arg-type]

    assert error_of(response) == "invalid_response"
    # Rejected by the intended check, not by an accident of the test response.
    assert [e["check"] for e in logged if e["event"] == "login_failed"] == [check]
    assert await _users(db_session) == []
    (failed,) = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert failed.details == {"provider": "saml:corp", "reason": "invalid_response"}


async def test_replayed_assertion_is_rejected(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, saml.idp)
    started = await start(saml)
    flow_cookie = saml.client.cookies[FLOW_COOKIE]
    saml_response = saml.idp.response(
        in_response_to=started.request_id,
        acs_url=ACS_URL,
        audience=sp_entity_id(),
        attributes=DEFAULT_ATTRIBUTES,
    )
    assert error_of(await post_acs(saml, saml_response, started.relay_state)) is None

    # An attacker who captured both the posted form and the flow cookie.
    saml.client.cookies.clear()
    saml.client.cookies.set(FLOW_COOKIE, flow_cookie)
    replayed = await post_acs(saml, saml_response, started.relay_state)

    assert error_of(replayed) == "invalid_response"
    assert len(await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)) == 1


async def test_same_assertion_id_from_another_login_is_rejected(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)

    assert error_of(await sign_in(saml, assertion_id="_fixed")) is None
    saml.client.cookies.clear()

    assert error_of(await sign_in(saml, assertion_id="_fixed")) == "invalid_response"


async def test_idp_error_status(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, saml.idp)

    error = await _rejected(saml, db_session, status="urn:oasis:names:tc:SAML:2.0:status:Responder")

    assert error == "idp_error"


async def test_xxe_and_entity_expansion_are_rejected(
    saml: SAMLTestApp, db_session: AsyncSession, tmp_path: object
) -> None:
    await add_provider(db_session, saml.idp)
    doctype = (
        '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">'
        '<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    )

    error = await _rejected(
        saml, db_session, doctype=doctype, tamper=("Erika Mustermann", "&xxe;&b;")
    )

    assert error == "invalid_response"


async def test_garbage_response_is_rejected(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, saml.idp)
    started = await start(saml)

    for garbage in ["not base64!", "PG5vdC14bWw+", ""]:
        saml.client.cookies.set(FLOW_COOKIE, started.response.cookies[FLOW_COOKIE])
        response = await post_acs(saml, garbage, started.relay_state)
        assert error_of(response) == "invalid_response"


async def test_relay_state_must_match_flow_cookie(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session, saml.idp)
    started = await start(saml)
    saml_response = saml.idp.response(
        in_response_to=started.request_id, acs_url=ACS_URL, audience=sp_entity_id()
    )

    response = await post_acs(saml, saml_response, "forged-relay-state")

    assert error_of(response) == "state_invalid"
    assert await _users(db_session) == []


async def test_response_without_flow_cookie_is_rejected(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    """Login CSRF: a response for the attacker's account posted into a victim's browser."""
    await add_provider(db_session, saml.idp)
    started = await start(saml)
    saml_response = saml.idp.response(
        in_response_to=started.request_id, acs_url=ACS_URL, audience=sp_entity_id()
    )
    saml.client.cookies.clear()

    response = await post_acs(saml, saml_response, started.relay_state)

    assert error_of(response) == "state_invalid"
    assert await _users(db_session) == []


async def test_acs_requires_form_post(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    await add_provider(db_session, saml.idp)
    await start(saml)

    response = await saml.client.post(
        "/auth/saml/acs", json={"SAMLResponse": "x", "RelayState": "y"}
    )

    assert error_of(response) == "state_invalid"

"""Admin API for SAML providers: metadata (URL, upload, explicit), presets, validation,
SP metadata, admin lockout and the audit log."""

from typing import Any

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.providers.saml.presets import NAMEID_PERSISTENT, NAMEID_TRANSIENT
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.saml.conftest import ACS_URL, PUBLIC_URL, SAMLTestApp, sp_entity_id
from tests.auth.saml.idp import IDP_ENTITY_ID, IDP_SSO_URL, make_key_pair

pytestmark = pytest.mark.db

BASE = "/admin/auth/saml/providers"
METADATA_URL = "https://idp.example.org/saml/metadata"


def _body(saml: SAMLTestApp, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "corp",
        "display_name": "Corp SSO",
        "metadata_xml": saml.idp.metadata(),
    }
    body.update(overrides)
    return body


async def _admin(saml: SAMLTestApp, db: AsyncSession) -> AsyncClient:
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(saml.client, "admin@example.org")).status_code == 200
    return saml.client


async def test_requires_admin(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    assert (await saml.client.get(BASE)).status_code == 401
    await make_local_user(db_session)
    await login(saml.client, "erika@example.org")

    assert (await saml.client.get(BASE)).status_code == 403
    assert (await saml.client.post(BASE, json=_body(saml))).status_code == 403


async def test_create_from_uploaded_metadata(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)

    created = await client.post(BASE, json=_body(saml, allowed_domains=["Example.org"]))

    assert created.status_code == 201, created.text
    data = created.json()
    assert "metadata_xml" not in data
    assert data["provider"] == "saml:corp"
    assert data["idp_entity_id"] == IDP_ENTITY_ID
    assert data["idp_sso_url"] == IDP_SSO_URL
    (certificate,) = data["idp_certificates"]
    assert len(certificate["fingerprint_sha256"]) == 64
    assert data["metadata_url"] is None
    assert data["sp_entity_id"] is None
    assert data["effective_sp_entity_id"] == sp_entity_id()
    assert data["redirect_uri"] == ACS_URL
    assert data["sp_metadata_url"] == f"{PUBLIC_URL}/api/auth/saml/corp/metadata"
    # Generic preset.
    assert data["name_id_format"] == NAMEID_PERSISTENT
    assert data["email_attribute"] == "email"
    assert data["groups_attribute"] == "groups"
    assert data["trust_email"] is False
    assert data["allowed_domains"] == ["example.org"]
    assert (await client.get(f"{BASE}/corp")).json()["idp_entity_id"] == IDP_ENTITY_ID
    assert [p["name"] for p in (await client.get(BASE)).json()] == ["corp"]


async def test_preset_defaults_and_explicit_values(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)

    entra = await client.post(BASE, json=_body(saml, preset="entra"))
    custom = await client.post(
        BASE,
        json=_body(saml, name="adfs", preset="adfs", groups_attribute=None, sp_entity_id="urn:x"),
    )

    assert entra.status_code == 201
    assert entra.json()["subject_attribute"] == (
        "http://schemas.microsoft.com/identity/claims/objectidentifier"
    )
    assert entra.json()["groups_attribute"] == (
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
    )
    assert custom.status_code == 201
    assert custom.json()["groups_attribute"] is None
    assert custom.json()["email_attribute"] == (
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
    )
    assert custom.json()["effective_sp_entity_id"] == "urn:x"


async def test_create_with_explicit_idp_settings(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)

    created = await client.post(
        BASE,
        json={
            "name": "corp",
            "display_name": "Corp SSO",
            "idp_entity_id": IDP_ENTITY_ID,
            "idp_sso_url": IDP_SSO_URL,
            "idp_certificates": [saml.idp.keys.cert_pem],
        },
    )

    assert created.status_code == 201, created.text
    assert len(created.json()["idp_certificates"]) == 1


async def test_create_and_refresh_from_metadata_url(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(METADATA_URL).mock(
            return_value=httpx.Response(200, text=saml.idp.metadata())
        )
        created = await client.post(
            BASE, json={"name": "corp", "display_name": "Corp", "metadata_url": METADATA_URL}
        )
        assert created.status_code == 201, created.text
        assert created.json()["metadata_refreshed_at"] is not None

        # Certificate rollover at the IdP.
        saml.idp.keys = make_key_pair("Rolled over")
        route.mock(return_value=httpx.Response(200, text=saml.idp.metadata()))
        refreshed = await client.post(f"{BASE}/corp/refresh-metadata")

    assert refreshed.status_code == 200
    old = created.json()["idp_certificates"][0]["fingerprint_sha256"]
    assert refreshed.json()["idp_certificates"][0]["fingerprint_sha256"] != old
    changes = [r.details["change"] for r in await audit_rows(db_session, "idp.config_changed")]
    assert changes == ["created", "metadata_refreshed"]


async def test_metadata_url_errors(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)
    with respx.mock(assert_all_called=False) as router:
        router.get(METADATA_URL).mock(return_value=httpx.Response(302, headers={"location": "/"}))
        redirected = await client.post(
            BASE, json={"name": "corp", "display_name": "Corp", "metadata_url": METADATA_URL}
        )
        router.get(METADATA_URL).mock(side_effect=httpx.ConnectError("down"))
        unreachable = await client.post(
            BASE, json={"name": "corp", "display_name": "Corp", "metadata_url": METADATA_URL}
        )
    plain_http = await client.post(
        BASE, json={"name": "corp", "display_name": "Corp", "metadata_url": "http://idp.example"}
    )

    assert redirected.status_code == 422
    assert redirected.json()["type"] == "urn:ollamail:problem:saml-metadata-invalid"
    assert unreachable.status_code == 422
    assert plain_http.status_code == 422
    assert (await client.get(BASE)).json() == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"metadata_xml": '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'},
            id="xxe",
        ),
        pytest.param({"metadata_xml": "<not-metadata/>"}, id="no-idp"),
        pytest.param({"metadata_xml": None}, id="no-idp-settings"),
        pytest.param(
            {"metadata_xml": None, "idp_entity_id": "x", "idp_sso_url": IDP_SSO_URL},
            id="no-certificate",
        ),
        pytest.param(
            {
                "metadata_xml": None,
                "idp_entity_id": "x",
                "idp_sso_url": IDP_SSO_URL,
                "idp_certificates": ["bm90IGEgY2VydGlmaWNhdGU="],
            },
            id="invalid-certificate",
        ),
        pytest.param(
            {
                "metadata_xml": None,
                "idp_entity_id": "x",
                "idp_sso_url": "http://idp.example.org/sso",
                "idp_certificates": [],
            },
            id="http-sso-url",
        ),
        pytest.param(
            {"name_id_format": NAMEID_TRANSIENT, "subject_attribute": None}, id="transient"
        ),
        pytest.param({"name": "Not Valid"}, id="bad-name"),
    ],
)
async def test_invalid_settings_are_rejected(
    saml: SAMLTestApp, db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    client = await _admin(saml, db_session)

    response = await client.post(BASE, json=_body(saml, **overrides))

    assert response.status_code == 422
    assert (await client.get(BASE)).json() == []


async def test_metadata_without_redirect_binding_is_rejected(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)
    xml = saml.idp.metadata(sso_binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST")

    response = await client.post(BASE, json=_body(saml, metadata_xml=xml))

    assert response.status_code == 422
    assert "HTTP-Redirect" in response.json()["detail"]


async def test_update_and_delete(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)
    await client.post(BASE, json=_body(saml))
    new_keys = make_key_pair("New")

    updated = await client.patch(
        f"{BASE}/corp",
        json={
            "display_name": "Renamed",
            "groups_attribute": None,
            "trust_email": True,
            "idp_certificates": [saml.idp.keys.cert_b64, new_keys.cert_b64],
        },
    )
    unchanged_idp = await client.patch(f"{BASE}/corp", json={"enabled": False})
    invalid = await client.patch(f"{BASE}/corp", json={"idp_certificates": []})
    deleted = await client.delete(f"{BASE}/corp")

    assert updated.status_code == 200, updated.text
    data = updated.json()
    assert data["display_name"] == "Renamed"
    assert data["groups_attribute"] is None
    assert data["email_attribute"] == "email"
    assert data["trust_email"] is True
    assert len(data["idp_certificates"]) == 2
    assert unchanged_idp.json()["enabled"] is False
    assert len(unchanged_idp.json()["idp_certificates"]) == 2
    assert invalid.status_code == 422
    assert deleted.status_code == 204
    assert (await client.get(f"{BASE}/corp")).status_code == 404
    rows = await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)
    assert [r.details for r in rows] == [
        {"kind": "saml", "change": "created"},
        {"kind": "saml", "change": "updated"},
        {"kind": "saml", "change": "updated"},
        {"kind": "saml", "change": "deleted"},
    ]
    assert all(r.actor_kind == "user" for r in rows)


async def test_refresh_without_metadata_url(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)
    await client.post(BASE, json=_body(saml))

    response = await client.post(f"{BASE}/corp/refresh-metadata")

    assert response.status_code == 422


async def test_duplicate_name(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)
    await client.post(BASE, json=_body(saml))

    assert (await client.post(BASE, json=_body(saml))).status_code == 409


async def test_disabling_the_only_admin_access_is_refused(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)
    assert (await client.post(BASE, json=_body(saml))).status_code == 201
    admin_id = await db_session.scalar(text("SELECT id FROM users"))
    await db_session.execute(
        text(
            "UPDATE auth_identities SET provider = 'saml:corp', subject = 'u-1', "
            "password_hash = NULL WHERE user_id = :id"
        ),
        {"id": admin_id},
    )
    await db_session.commit()

    disable = await client.patch(f"{BASE}/corp", json={"enabled": False})
    delete = await client.delete(f"{BASE}/corp")

    assert disable.status_code == 409
    assert disable.json()["type"] == "urn:ollamail:problem:admin-lockout"
    assert delete.status_code == 409
    assert (await client.get(f"{BASE}/corp")).json()["enabled"] is True


async def test_sp_metadata(saml: SAMLTestApp, db_session: AsyncSession) -> None:
    client = await _admin(saml, db_session)
    await client.post(BASE, json=_body(saml, enabled=False))
    client.cookies.clear()

    response = await client.get("/auth/saml/corp/metadata")
    missing = await client.get("/auth/saml/other/metadata")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/samlmetadata+xml")
    xml = response.text
    assert f'entityID="{sp_entity_id()}"' in xml
    assert f'Location="{ACS_URL}"' in xml
    assert "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" in xml
    assert missing.status_code == 404


async def test_settings_list_saml_as_provider_kind(
    saml: SAMLTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(saml, db_session)

    settings = (await client.get("/admin/auth/settings")).json()

    assert "saml" in settings["provider_kinds"]

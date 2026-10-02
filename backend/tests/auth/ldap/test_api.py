"""LDAP admin API and login, end to end against PostgreSQL and (marker ``ldap``) slapd."""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import audit_events
from app.auth.models import Identity
from app.auth.providers.ldap.client import LdapDirectoryClient, LdapUnavailableError
from app.auth.sessions import SESSION_COOKIE
from app.core.config import Settings
from app.users.models import User, UserRole
from tests.auth.conftest import login, make_local_user
from tests.auth.ldap.conftest import directory_settings
from tests.auth.ldap.slapd import SERVICE_PASSWORD, USER_PASSWORD, Slapd, group_dn

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("keyring")]

DIRECTORIES = "/auth/ldap/directories"


def _body(slapd: Slapd | None = None, **settings: Any) -> dict[str, Any]:
    return {
        "name": "corp",
        "display_name": "Corporate directory",
        "bind_password": SERVICE_PASSWORD,
        "settings": directory_settings(slapd, **settings).model_dump(mode="json"),
    }


async def _sign_in_admin(client: AsyncClient, db_session: AsyncSession) -> User:
    admin = await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(client, admin.email)).status_code == 200
    return admin


async def _create(client: AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(DIRECTORIES, json=body)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def _ldap_login(client: AsyncClient, username: str, password: str = USER_PASSWORD) -> Any:
    client.cookies.clear()
    return await client.post(
        "/auth/login/ldap/corp", json={"username": username, "password": password}
    )


# -- administration ----------------------------------------------------------------------


async def test_directory_admin_requires_admin(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    assert (await db_client.get(DIRECTORIES)).status_code == 401
    user = await make_local_user(db_session)
    await login(db_client, user.email)

    assert (await db_client.get(DIRECTORIES)).status_code == 403
    assert (await db_client.post(DIRECTORIES, json=_body())).status_code == 403
    assert (await db_client.post(f"{DIRECTORIES}/corp/test")).status_code == 403


async def test_create_read_update_delete(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _sign_in_admin(db_client, db_session)

    created = await _create(db_client, _body())

    assert created["provider"] == "ldap:corp"
    assert created["bind_password_set"] is True
    assert "bind_password" not in created
    assert SERVICE_PASSWORD not in str(created)
    assert created["settings"]["subject_attribute"] == "entryUUID"
    stored = await db_session.scalar(
        text("SELECT bind_password FROM auth_ldap_directories WHERE name = 'corp'")
    )
    assert stored and SERVICE_PASSWORD not in stored  # encrypted at rest

    assert (await db_client.post(DIRECTORIES, json=_body())).status_code == 409
    assert [d["name"] for d in (await db_client.get(DIRECTORIES)).json()] == ["corp"]

    update = _body(email_attribute="mailAlternate")
    del update["name"], update["bind_password"]
    update["enabled"] = False
    updated = await db_client.put(f"{DIRECTORIES}/corp", json=update)
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["settings"]["email_attribute"] == "mailAlternate"
    # The bind password was kept.
    assert (
        await db_session.scalar(
            text("SELECT bind_password FROM auth_ldap_directories WHERE name = 'corp'")
        )
        == stored
    )

    assert (await db_client.delete(f"{DIRECTORIES}/corp")).status_code == 204
    assert (await db_client.get(f"{DIRECTORIES}/corp")).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {**_body(), "name": "Corp"},
        {**_body(), "name": "x" * 33},
        {**_body(), "bind_password": ""},
        {**_body(), "settings": {**_body()["settings"], "user_filter": "(uid=*)"}},
    ],
)
async def test_invalid_directory_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession, body: dict[str, Any]
) -> None:
    await _sign_in_admin(db_client, db_session)
    assert (await db_client.post(DIRECTORIES, json=body)).status_code == 422


async def test_plaintext_needs_explicit_setting(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _sign_in_admin(db_client, db_session)
    body = _body(tls_mode="none", server_urls=["ldap://ldap.example.org"])

    rejected = await db_client.post(DIRECTORIES, json=body)

    assert rejected.status_code == 422
    assert rejected.json()["type"] == "urn:ollamail:problem:ldap-plaintext-disabled"
    settings.auth.ldap_allow_plaintext = True
    assert (await db_client.post(DIRECTORIES, json=body)).status_code == 201


async def test_providers_list_enabled_directories(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, _body())
    await _create(db_client, {**_body(), "name": "off", "enabled": False})

    providers = (await db_client.get("/auth/providers")).json()["providers"]

    assert providers == [
        {
            "name": "ldap:corp",
            "display_name": "Corporate directory",
            "kind": "password",
            "login_path": None,
        }
    ]


async def test_unknown_or_disabled_directory(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, {**_body(), "enabled": False})

    assert (await _ldap_login(db_client, "erika")).status_code == 404
    response = await db_client.post(
        "/auth/login/ldap/nope", json={"username": "erika", "password": "x"}
    )
    assert response.status_code == 404


async def test_unreachable_directory_is_503_not_401(
    db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, _body())

    def unavailable(self: LdapDirectoryClient, login: str, password: str) -> None:
        raise LdapUnavailableError("unreachable")

    monkeypatch.setattr(LdapDirectoryClient, "authenticate", unavailable)

    response = await _ldap_login(db_client, "erika")
    assert response.status_code == 503
    assert response.json()["type"] == "urn:ollamail:problem:directory-unavailable"


async def test_plaintext_switched_off_later_blocks_login(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    settings.auth.ldap_allow_plaintext = True
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, _body(tls_mode="none", server_urls=["ldap://ldap.example.org"]))
    settings.auth.ldap_allow_plaintext = False

    assert (await _ldap_login(db_client, "erika")).status_code == 503


# -- login against slapd -----------------------------------------------------------------


@pytest.fixture
async def corp(db_client: AsyncClient, db_session: AsyncSession, slapd: Slapd) -> AsyncClient:
    """``db_client`` with the directory ``corp`` (slapd) configured, signed out."""
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, _body(slapd))
    return db_client


async def _identity(db_session: AsyncSession, user_id: Any) -> Identity:
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == user_id))
    assert identity is not None
    return identity


@pytest.mark.ldap
async def test_first_login_provisions_user(corp: AsyncClient, db_session: AsyncSession) -> None:
    response = await _ldap_login(corp, "erika")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "erika.mustermann@example.org"
    assert body["display_name"] == "Erika Mustermann"
    assert body["role"] == "user"
    assert SESSION_COOKIE in corp.cookies
    me = await corp.get("/auth/me")
    assert me.json()["id"] == body["id"]
    identity = await _identity(db_session, body["id"])
    assert identity.provider == "ldap:corp"
    assert identity.password_hash is None
    sessions = (await corp.get("/auth/sessions")).json()
    assert sessions[0]["provider"] == "ldap:corp"

    again = await _ldap_login(corp, "ERIKA")
    assert again.json()["id"] == body["id"]
    count = await db_session.scalar(
        text("SELECT count(*) FROM users WHERE email = 'erika.mustermann@example.org'")
    )
    assert count == 1


@pytest.mark.ldap
async def test_wrong_password_unknown_and_disabled_look_the_same(corp: AsyncClient) -> None:
    responses = [
        await _ldap_login(corp, "erika", "wrong password"),
        await _ldap_login(corp, "nobody"),
        await _ldap_login(corp, "disabled"),
        await _ldap_login(corp, "*"),
    ]

    assert {r.status_code for r in responses} == {401}
    assert len({r.json()["detail"] for r in responses}) == 1
    assert SESSION_COOKIE not in corp.cookies


@pytest.mark.ldap
async def test_empty_password_is_rejected(corp: AsyncClient) -> None:
    assert (await _ldap_login(corp, "erika", "")).status_code == 422
    assert (await _ldap_login(corp, "erika", "\x00")).status_code == 401


@pytest.mark.ldap
async def test_account_lockout(corp: AsyncClient, settings: Settings) -> None:
    for _ in range(settings.auth.login_max_attempts):
        assert (await _ldap_login(corp, "erika", "wrong password")).status_code == 401

    locked = await _ldap_login(corp, "erika")

    assert locked.status_code == 429
    assert locked.json()["retry_after"] > 0
    # Other accounts of the directory are not affected.
    assert (await _ldap_login(corp, "max")).status_code == 200


@pytest.mark.ldap
async def test_group_mapping_sets_role(
    corp: AsyncClient, db_session: AsyncSession, slapd: Slapd
) -> None:
    update = _body(slapd, admin_groups=[group_dn("admins")])
    del update["name"], update["bind_password"]
    await corp.put(f"{DIRECTORIES}/corp", json=update)

    # erika is in admins through the nested group staff.
    erika = await _ldap_login(corp, "erika")
    assert erika.json()["role"] == "admin"

    corp.cookies.clear()
    await login(corp, "admin@example.org")
    update["settings"]["admin_groups"] = [group_dn("other")]
    await corp.put(f"{DIRECTORIES}/corp", json=update)

    demoted = await _ldap_login(corp, "erika")
    assert demoted.json()["role"] == "user"


@pytest.mark.ldap
async def test_allowed_groups(corp: AsyncClient, slapd: Slapd) -> None:
    update = _body(slapd, allowed_groups=[group_dn("other")])
    del update["name"], update["bind_password"]
    await corp.put(f"{DIRECTORIES}/corp", json=update)

    assert (await _ldap_login(corp, "erika")).status_code == 401
    assert (await _ldap_login(corp, "max")).status_code == 200


@pytest.mark.ldap
async def test_existing_local_account_is_not_taken_over(
    corp: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, "max@example.org")

    response = await _ldap_login(corp, "max")

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:account-exists"


@pytest.mark.ldap
async def test_user_without_email(corp: AsyncClient) -> None:
    response = await _ldap_login(corp, "nomail")
    assert response.status_code == 403
    assert response.json()["type"] == "urn:ollamail:problem:missing-email"


@pytest.mark.ldap
async def test_deactivated_user_cannot_sign_in(corp: AsyncClient, db_session: AsyncSession) -> None:
    user_id = (await _ldap_login(corp, "erika")).json()["id"]
    user = await db_session.get(User, user_id)
    assert user is not None
    user.is_active = False
    await db_session.commit()

    assert (await _ldap_login(corp, "erika")).status_code == 401


@pytest.mark.ldap
async def test_deleting_directory_unlinks_identities(
    corp: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = (await _ldap_login(corp, "erika")).json()["id"]
    corp.cookies.clear()
    await login(corp, "admin@example.org")

    assert (await corp.delete(f"{DIRECTORIES}/corp")).status_code == 204

    assert await db_session.get(User, user_id) is not None
    remaining = await db_session.scalar(select(Identity).where(Identity.provider == "ldap:corp"))
    assert remaining is None


@pytest.mark.ldap
async def test_connection_test_and_user_lookup(
    db_client: AsyncClient, db_session: AsyncSession, slapd: Slapd
) -> None:
    await _sign_in_admin(db_client, db_session)
    await _create(
        db_client,
        _body(
            slapd,
            server_urls=["ldaps://127.0.0.1:1", slapd.ldaps_url],
            admin_groups=[group_dn("admins")],
            connect_timeout=1,
        ),
    )

    test = (await db_client.post(f"{DIRECTORIES}/corp/test")).json()
    assert test["ok"] is True
    assert [(s["ok"], s["error"]) for s in test["servers"]] == [
        (False, "unreachable"),
        (True, None),
    ]

    lookup = (await db_client.post(f"{DIRECTORIES}/corp/test-user", json={"login": "erika"})).json()
    assert lookup["found"] is True
    assert lookup["dn"] == "uid=erika,ou=people,dc=example,dc=org"
    assert lookup["groups"] == sorted([group_dn("admins"), group_dn("staff")])
    assert lookup["role"] == "admin"
    assert lookup["allowed"] is True
    assert lookup["disabled"] is False

    missing = await db_client.post(f"{DIRECTORIES}/corp/test-user", json={"login": "*"})
    assert missing.json()["found"] is False


@pytest.mark.ldap
async def test_connection_test_reports_wrong_service_password(
    db_client: AsyncClient, db_session: AsyncSession, slapd: Slapd
) -> None:
    await _sign_in_admin(db_client, db_session)
    await _create(db_client, {**_body(slapd), "bind_password": "wrong"})

    test = (await db_client.post(f"{DIRECTORIES}/corp/test")).json()
    lookup = (await db_client.post(f"{DIRECTORIES}/corp/test-user", json={"login": "erika"})).json()

    assert test["ok"] is False
    assert test["servers"][0]["error"] == "service_bind_failed"
    assert lookup == {**lookup, "found": False, "error": "service_bind_failed"}


@pytest.mark.ldap
async def test_login_and_configuration_are_audited(
    corp: AsyncClient, db_session: AsyncSession, slapd: Slapd
) -> None:
    update = _body(slapd, admin_groups=[group_dn("admins")])
    del update["name"], update["bind_password"]
    await corp.put(f"{DIRECTORIES}/corp", json=update)
    await _ldap_login(corp, "erika", "wrong password")
    user_id = (await _ldap_login(corp, "erika")).json()["id"]

    rows = (
        await db_session.execute(
            select(audit_events.c.action, audit_events.c.details).order_by(audit_events.c.id)
        )
    ).all()
    events = [(row.action, row.details) for row in rows]

    assert ("idp.config_changed", {"kind": "ldap", "change": "created"}) in events
    assert ("idp.config_changed", {"kind": "ldap", "change": "updated"}) in events
    assert (
        "auth.login_failed",
        {"provider": "ldap:corp", "reason": "invalid_credentials"},
    ) in events
    assert ("user.created", {"role": "admin", "provider": "ldap:corp"}) in events
    assert ("auth.login_succeeded", {"provider": "ldap:corp"}) in events
    # Neither the login name nor the address ends up in the audit log.
    assert "erika" not in str(events)
    assert user_id

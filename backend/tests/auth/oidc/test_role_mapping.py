"""Group → role mapping and admin lockout protection with a real OIDC login (#33)."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.oidc.conftest import OIDCTestApp, add_provider, sign_in

pytestmark = pytest.mark.db

MAPPING = "/admin/auth/role-mapping"


async def _admin(oidc: OIDCTestApp, db: AsyncSession) -> AsyncClient:
    await make_local_user(db, "admin@example.org", role=UserRole.ADMIN)
    assert (await login(oidc.client, "admin@example.org")).status_code == 200
    return oidc.client


async def _role(db: AsyncSession, email: str) -> UserRole:
    role = await db.scalar(select(User.role).where(User.email == email))
    assert role is not None
    return role


async def test_group_change_at_the_idp_changes_the_role_at_next_login(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    await add_provider(db_session)
    saved = await client.put(
        MAPPING,
        json={
            "enabled": True,
            "default_role": "user",
            "rules": [{"group": "Mail-Admins", "provider": "oidc:test", "role": "admin"}],
        },
    )
    assert saved.status_code == 200
    await client.post("/auth/logout")

    # The mock IdP reports the groups "staff" and "mail-admins" (case differs).
    await sign_in(oidc)
    assert await _role(db_session, "erika@example.org") is UserRole.ADMIN

    oidc.idp.claims = {"groups": ["staff"]}
    await sign_in(oidc)
    assert await _role(db_session, "erika@example.org") is UserRole.USER

    oidc.idp.claims = {"groups": ["staff", "mail-admins"]}
    await sign_in(oidc)
    assert await _role(db_session, "erika@example.org") is UserRole.ADMIN
    changes = await audit_rows(db_session, AuditAction.USER_ROLE_CHANGED)
    assert [(row.details["from_role"], row.details["to_role"]) for row in changes] == [
        ("admin", "user"),
        ("user", "admin"),
    ]


async def test_mapping_off_keeps_manually_assigned_roles(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await sign_in(oidc)
    user = await db_session.scalar(select(User).where(User.email == "erika@example.org"))
    assert user is not None and user.role is UserRole.USER
    user.role = UserRole.ADMIN
    await db_session.commit()

    await sign_in(oidc)

    assert await _role(db_session, "erika@example.org") is UserRole.ADMIN


async def test_default_role_and_rules_for_other_providers(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    await add_provider(db_session)
    await client.put(
        MAPPING,
        json={
            "enabled": True,
            "default_role": "user",
            "rules": [{"group": "mail-admins", "provider": "ldap:corp", "role": "admin"}],
        },
    )

    await sign_in(oidc)

    assert await _role(db_session, "erika@example.org") is UserRole.USER


async def test_mapping_api_round_trip_test_and_audit(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    body = {
        "enabled": True,
        "default_role": "user",
        "rules": [
            {"group": "mail-admins", "provider": None, "role": "admin"},
            {"group": "staff", "provider": "oidc:test", "role": "user"},
        ],
    }

    saved = (await client.put(MAPPING, json=body)).json()
    read = (await client.get(MAPPING)).json()
    test = await client.post(
        f"{MAPPING}/test", json={"provider": "oidc:test", "groups": ["MAIL-ADMINS", "x"]}
    )
    duplicate = await client.put(
        MAPPING,
        json={**body, "rules": [body["rules"][0], {**body["rules"][0], "group": "Mail-Admins"}]},
    )

    assert saved == read
    assert [(r["group"], r["provider"], r["role"]) for r in read["rules"]] == [
        ("mail-admins", None, "admin"),
        ("staff", "oidc:test", "user"),
    ]
    assert test.json() == {"role": "admin", "matched_groups": ["mail-admins"]}
    assert duplicate.status_code == 422
    (row,) = await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)
    assert row.target_type == "settings"
    assert row.details["kind"] == "role_mapping"
    assert (row.details["rules"], row.details["admin_rules"]) == (2, 1)


async def test_mapping_requires_admin(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    await make_local_user(db_session)
    await login(oidc.client, "erika@example.org")

    assert (await oidc.client.get(MAPPING)).status_code == 403
    assert (await oidc.client.put(MAPPING, json={"enabled": True})).status_code == 403


async def test_local_login_can_be_disabled_once_an_sso_admin_exists(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    client = await _admin(oidc, db_session)
    await add_provider(db_session)

    blocked = await client.patch("/admin/auth/settings", json={"local_login_enabled": False})
    assert blocked.status_code == 409
    assert blocked.json()["type"] == "urn:ollamail:problem:admin-lockout"

    # Erika signs in via SSO and is made admin; now an admin access remains.
    await client.post("/auth/logout")
    await sign_in(oidc)
    erika = await db_session.scalar(select(User).where(User.email == "erika@example.org"))
    assert erika is not None
    erika.role = UserRole.ADMIN
    await db_session.commit()

    settings = (await oidc.client.get("/admin/auth/settings")).json()
    assert settings["admin_access"] == {"usable_admins": 2, "own_providers": ["oidc:test"]}
    disabled = await oidc.client.patch("/admin/auth/settings", json={"local_login_enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["local_login_enabled"] is False
    assert disabled.json()["admin_access"]["usable_admins"] == 1

    providers = (await oidc.client.get("/auth/providers")).json()
    assert providers["local_login"] is False
    await oidc.client.post("/auth/logout")
    local = await login(oidc.client, "admin@example.org")
    assert local.status_code == 403
    assert local.json()["type"] == "urn:ollamail:problem:local-login-disabled"
    (row,) = await audit_rows(db_session, AuditAction.IDP_CONFIG_CHANGED)
    assert row.details == {"kind": "local", "change": "disabled"}


async def test_disabling_the_only_admin_provider_is_refused(
    oidc: OIDCTestApp, db_session: AsyncSession
) -> None:
    await add_provider(db_session)
    await sign_in(oidc)
    erika = await db_session.scalar(select(User).where(User.email == "erika@example.org"))
    assert erika is not None
    erika.role = UserRole.ADMIN
    await db_session.commit()
    # Erika (SSO) is the only admin.
    disable = await oidc.client.patch("/admin/auth/oidc/providers/test", json={"enabled": False})
    delete = await oidc.client.delete("/admin/auth/oidc/providers/test")

    assert disable.status_code == 409
    assert delete.status_code == 409
    assert (await oidc.client.get("/admin/auth/oidc/providers/test")).json()["enabled"] is True


async def test_connection_test(oidc: OIDCTestApp, db_session: AsyncSession) -> None:
    client = await _admin(oidc, db_session)
    await add_provider(db_session)
    await add_provider(db_session, "broken", issuer="https://unreachable.invalid")

    ok = (await client.post("/admin/auth/oidc/providers/test/test")).json()
    broken = (await client.post("/admin/auth/oidc/providers/broken/test")).json()

    assert ok["ok"] is True
    assert ok["signing_keys"] >= 1
    assert broken == {
        "ok": False,
        "error": "provider_unavailable",
        "issuer": None,
        "authorization_endpoint": None,
        "token_endpoint": None,
        "end_session_supported": False,
        "signing_keys": 0,
    }

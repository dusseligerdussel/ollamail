"""Passkeys with a simulated authenticator: registration, second factor, passwordless."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.mfa.models import Passkey
from app.auth.sessions import SESSION_COOKIE
from app.core.config import AuthSettings, Settings
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.auth.mfa.authenticator import SoftAuthenticator
from tests.auth.mfa.conftest import ORIGIN, add_passkey, password_step

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"


async def _signed_in(client: AsyncClient, db: AsyncSession) -> None:
    await make_local_user(db, EMAIL)
    assert (await login(client, EMAIL)).status_code == 200


async def test_register_options_come_from_the_configuration(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)

    options = (await db_client.post("/auth/mfa/passkeys/options")).json()

    assert options["rp"] == {"id": "test", "name": "ollamail"}
    assert options["attestation"] == "none"
    assert options["authenticatorSelection"]["residentKey"] == "preferred"
    # The user handle is the opaque user ID, not the e-mail address.
    assert EMAIL not in options["user"]["id"]
    assert len(options["challenge"]) >= 43


async def test_registration_stores_the_passkey_and_issues_recovery_codes(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)

    _, body = await add_passkey(db_client, "  Laptop  ")

    assert body["passkey"]["name"] == "Laptop"
    assert len(body["recovery_codes"]) == 10
    assert body["user"] is None
    status = (await db_client.get("/auth/mfa")).json()
    assert [p["name"] for p in status["passkeys"]] == ["Laptop"]
    assert status["recovery_codes_remaining"] == 10
    # The second passkey adds no new codes; the first one is excluded in the options.
    options = (await db_client.post("/auth/mfa/passkeys/options")).json()
    assert len(options["excludeCredentials"]) == 1
    _, second = await add_passkey(db_client, "Phone")
    assert second["recovery_codes"] is None
    (enabled, _) = await audit_rows(db_session, AuditAction.MFA_ENABLED)
    assert enabled.details["method"] == "webauthn"


async def test_registration_with_a_foreign_origin_or_rp_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    authenticator = SoftAuthenticator(origin="https://evil.example")

    options = (await db_client.post("/auth/mfa/passkeys/options")).json()
    foreign_origin = await db_client.post(
        "/auth/mfa/passkeys", json={"name": "X", "credential": authenticator.create(options)}
    )
    options = (await db_client.post("/auth/mfa/passkeys/options")).json()
    foreign_rp = await db_client.post(
        "/auth/mfa/passkeys",
        json={
            "name": "X",
            "credential": SoftAuthenticator(origin=ORIGIN).create(options, rp_id="evil.example"),
        },
    )
    # The challenge is single use.
    replay = await db_client.post(
        "/auth/mfa/passkeys",
        json={"name": "X", "credential": SoftAuthenticator(origin=ORIGIN).create(options)},
    )

    assert foreign_origin.status_code == 400
    assert foreign_rp.status_code == 400
    assert replay.status_code == 400
    assert (await db_session.scalars(select(Passkey))).all() == []


async def test_passkey_as_second_factor(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)
    authenticator, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")

    challenge = await password_step(db_client, EMAIL)
    options = (await db_client.post("/auth/mfa/verify/passkey/options")).json()
    response = await db_client.post(
        "/auth/mfa/verify/passkey",
        json={"credential": authenticator.get(options, user_verified=False)},
    )

    assert challenge.json()["methods"] == ["webauthn", "recovery"]
    assert [c["id"] for c in options["allowCredentials"]] == [authenticator.get(options)["id"]]
    assert options["userVerification"] == "discouraged"
    assert response.status_code == 200, response.text
    assert SESSION_COOKIE in db_client.cookies
    passkey = await db_session.scalar(select(Passkey))
    assert passkey is not None
    assert passkey.last_used_at is not None
    assert passkey.sign_count == 1


async def test_a_cloned_authenticator_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    authenticator, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)
    options = (await db_client.post("/auth/mfa/verify/passkey/options")).json()
    assert (
        await db_client.post(
            "/auth/mfa/verify/passkey", json={"credential": authenticator.get(options)}
        )
    ).status_code == 200
    await db_client.post("/auth/logout")

    # A copy whose signature counter did not advance.
    authenticator.sign_count = 0
    await password_step(db_client, EMAIL)
    options = (await db_client.post("/auth/mfa/verify/passkey/options")).json()
    response = await db_client.post(
        "/auth/mfa/verify/passkey", json={"credential": authenticator.get(options)}
    )

    assert response.status_code == 401


async def test_another_users_passkey_is_no_second_factor(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _signed_in(db_client, db_session)
    await add_passkey(db_client)
    await db_client.post("/auth/logout")
    await make_local_user(db_session, "max@example.org")
    assert (await login(db_client, "max@example.org")).status_code == 200
    stranger, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")

    await password_step(db_client, EMAIL)
    options = (await db_client.post("/auth/mfa/verify/passkey/options")).json()
    response = await db_client.post(
        "/auth/mfa/verify/passkey", json={"credential": stranger.get(options)}
    )

    assert response.status_code == 401
    assert SESSION_COOKIE not in db_client.cookies


async def test_passwordless_sign_in(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)
    authenticator, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")

    providers = (await db_client.get("/auth/providers")).json()
    options = (await db_client.post("/auth/passkey/options")).json()
    response = await db_client.post(
        "/auth/passkey/login", json={"credential": authenticator.get(options)}
    )

    assert providers["passkey_login"] is True
    assert options["allowCredentials"] == []
    assert options["userVerification"] == "required"
    assert response.status_code == 200, response.text
    assert response.json()["email"] == EMAIL
    succeeded = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert succeeded[-1].details == {"provider": "local", "mfa": "webauthn", "passwordless": True}


async def test_passwordless_needs_user_verification_and_a_fresh_challenge(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    authenticator, _ = await add_passkey(db_client)
    await db_client.post("/auth/logout")

    options = (await db_client.post("/auth/passkey/options")).json()
    unverified = await db_client.post(
        "/auth/passkey/login",
        json={"credential": authenticator.get(options, user_verified=False)},
    )
    replay = await db_client.post(
        "/auth/passkey/login", json={"credential": authenticator.get(options)}
    )

    assert unverified.status_code == 401
    assert replay.status_code == 401
    assert SESSION_COOKIE not in db_client.cookies
    failed = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert [row.details["reason"] for row in failed] == ["invalid_passkey"]


async def test_passkeys_need_a_configured_relying_party(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await _signed_in(db_client, db_session)
    settings.auth = AuthSettings()

    status = (await db_client.get("/auth/mfa")).json()
    options = await db_client.post("/auth/mfa/passkeys/options")

    assert status["passkeys_configured"] is False
    assert options.status_code == 409
    assert options.json()["type"] == "urn:ollamail:problem:passkeys-not-configured"
    assert (await db_client.get("/auth/providers")).json()["passkey_login"] is False


async def test_removing_a_passkey(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)
    _, body = await add_passkey(db_client)

    removed = await db_client.delete(f"/auth/mfa/passkeys/{body['passkey']['id']}")
    again = await db_client.delete(f"/auth/mfa/passkeys/{body['passkey']['id']}")

    assert removed.status_code == 204
    assert again.status_code == 404
    status = (await db_client.get("/auth/mfa")).json()
    # Without a factor the recovery codes are gone as well.
    assert status["passkeys"] == []
    assert status["recovery_codes_remaining"] == 0
    (disabled,) = await audit_rows(db_session, AuditAction.MFA_DISABLED)
    assert disabled.details["via"] == "self"

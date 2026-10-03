"""Recovery codes stay valid across a rotation of ``OLLAMAIL_SECRET_KEY`` (#144)."""

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.mfa import recovery
from app.auth.mfa.models import RecoveryCode
from app.core.config import SecuritySettings, Settings
from app.core.crypto import generate_key
from tests.auth.conftest import login, make_local_user
from tests.auth.mfa.conftest import enable_totp, password_step

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"


def test_old_keys_give_further_candidate_hashes() -> None:
    old_key = generate_key()
    old = SecuritySettings(secret_key=SecretStr(old_key))
    rotated = SecuritySettings(
        secret_key=SecretStr(generate_key()), secret_keys_old=[SecretStr(old_key)]
    )

    hashes = recovery.code_hashes(rotated, "abcde-fghjk")

    assert hashes[0] == recovery.code_hash(rotated, "abcde-fghjk")
    assert hashes[1:] == [recovery.code_hash(old, "abcde-fghjk")]
    # Formatting does not matter, as for the current key.
    assert recovery.code_hashes(rotated, "ABCDE FGHJK") == hashes


async def _rotate(settings: Settings, *, keep_old: bool) -> None:
    """Steps 1 and 2 of the documented rotation (deploy/README.md); ``keep_old=False`` is
    the state after step 4."""
    previous = settings.security.secret_key
    settings.security = SecuritySettings(
        secret_key=SecretStr(generate_key()),
        secret_keys_old=[previous] if keep_old and previous else [],
    )


async def test_codes_from_before_a_key_rotation_still_sign_in(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session, EMAIL)
    assert (await login(db_client, EMAIL)).status_code == 200
    _, codes = await enable_totp(db_client)
    await db_client.post("/auth/logout")
    await _rotate(settings, keep_old=True)

    await password_step(db_client, EMAIL)
    response = await db_client.post(
        "/auth/mfa/verify", json={"method": "recovery", "code": codes[0]}
    )

    assert response.status_code == 200, response.text
    # The used code is re-hashed under the current key.
    used = await db_session.scalar(select(RecoveryCode).where(RecoveryCode.used_at.is_not(None)))
    assert used is not None
    assert used.code_hash == recovery.code_hash(settings.security, codes[0])
    # Used is used, whichever key matches.
    await db_client.post("/auth/logout")
    await password_step(db_client, EMAIL)
    again = await db_client.post("/auth/mfa/verify", json={"method": "recovery", "code": codes[0]})
    assert again.status_code == 401


async def test_codes_need_the_old_key_until_new_ones_exist(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    """Documents the limit: without the old key, codes from before cannot be checked."""
    await make_local_user(db_session, EMAIL)
    assert (await login(db_client, EMAIL)).status_code == 200
    _, codes = await enable_totp(db_client)
    await db_client.post("/auth/logout")
    await _rotate(settings, keep_old=False)

    await password_step(db_client, EMAIL)
    response = await db_client.post(
        "/auth/mfa/verify", json={"method": "recovery", "code": codes[0]}
    )

    assert response.status_code == 401

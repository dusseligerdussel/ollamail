"""``reset-password`` keeps second factors unless ``--reset-2fa`` is given (audit-logged)."""

import asyncio

import pytest
from click.testing import Result
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.audit.models import audit_events
from app.auth.mfa.models import Passkey, RecoveryCode, TotpFactor
from app.cli import cli
from app.core.config import get_settings
from app.core.crypto import KeyRing, decode_key, generate_key, set_keyring
from app.core.ids import uuid7
from app.users.models import User, UserRole
from tests.auth.conftest import PASSWORD

pytestmark = pytest.mark.db

runner = CliRunner()


async def _invoke(args: list[str]) -> Result:
    get_settings.cache_clear()
    set_keyring(None)
    return await asyncio.to_thread(runner.invoke, cli, args, input=f"{PASSWORD}\n{PASSWORD}\n")


async def test_reset_password_with_and_without_reset_2fa(
    monkeypatch: pytest.MonkeyPatch, scratch_database: str
) -> None:
    key = generate_key()
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", scratch_database)
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", key)
    set_keyring(KeyRing(decode_key(key)))
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    user_id = uuid7()
    async with engine.begin() as connection:
        await connection.execute(
            insert(User).values(
                id=user_id,
                email="admin@example.org",
                display_name="Admin",
                role=UserRole.ADMIN,
                language="en",
                timezone="UTC",
                is_active=True,
            )
        )
    async with engine.begin() as connection:
        await connection.execute(
            insert(TotpFactor).values(id=uuid7(), user_id=user_id, secret="JBSWY3DPEHPK3PXP")
        )
        await connection.execute(
            insert(Passkey).values(
                id=uuid7(),
                user_id=user_id,
                credential_id=b"credential",
                public_key=b"key",
                sign_count=0,
                name="Laptop",
                backed_up=False,
            )
        )
        await connection.execute(
            insert(RecoveryCode).values(id=uuid7(), user_id=user_id, code_hash=bytes(32))
        )
    args = ["reset-password", "--email", "admin@example.org"]

    kept = await _invoke(args)
    async with engine.connect() as connection:
        totp_after_plain_reset = await connection.scalar(
            select(func.count()).select_from(TotpFactor)
        )
    reset = await _invoke([*args, "--reset-2fa"])

    assert kept.exit_code == 0, kept.output
    assert "still active" in kept.stdout
    assert totp_after_plain_reset == 1
    assert reset.exit_code == 0, reset.output
    assert "has been removed" in reset.stdout
    async with engine.connect() as connection:
        remaining = [
            await connection.scalar(select(func.count()).select_from(model))
            for model in (TotpFactor, Passkey, RecoveryCode)
        ]
        events = (
            await connection.execute(
                select(audit_events.c.action, audit_events.c.actor_kind, audit_events.c.details)
            )
        ).all()
    await engine.dispose()
    set_keyring(None)
    assert remaining == [0, 0, 0]
    (mfa_event,) = [row for row in events if row.action == "auth.mfa_disabled"]
    assert mfa_event.actor_kind == "system"
    assert mfa_event.details == {"via": "cli", "totp": 1, "passkeys": 1, "recovery_codes": 1}

import asyncio

import pytest
from click.testing import Result
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app.audit.models import audit_events
from app.auth.models import AuthPolicy, Identity
from app.auth.passwords import verify_password
from app.auth.setup import setup_token
from app.cli import cli
from app.core.config import get_settings
from app.core.crypto import generate_key, set_keyring
from app.core.ids import uuid7
from app.users.models import User, UserRole
from tests.auth.conftest import PASSWORD

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    get_settings.cache_clear()
    set_keyring(None)


def test_setup_token_matches_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", generate_key())

    result = runner.invoke(cli, ["setup-token"])

    assert result.exit_code == 0
    # Log lines go to stdout as well; the token is the last line.
    assert result.stdout.strip().splitlines()[-1] == setup_token(get_settings())


def test_setup_token_needs_the_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMAIL_SECRET_KEY", raising=False)

    result = runner.invoke(cli, ["setup-token"])

    assert result.exit_code == 1


@pytest.mark.db
async def test_create_admin(monkeypatch: pytest.MonkeyPatch, scratch_database: str) -> None:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", scratch_database)
    args = ["create-admin", "--email", "Root@Example.org", "--display-name", "Root"]

    created = await _invoke(args, f"{PASSWORD}\n{PASSWORD}\n")
    duplicate = await _invoke(args, f"{PASSWORD}\n{PASSWORD}\n")
    weak = await _invoke(
        ["create-admin", "--email", "x@example.org", "--display-name", "X"], "short\nshort\n"
    )

    assert created.exit_code == 0, created.output
    assert duplicate.exit_code == 1
    assert "already exists" in duplicate.stderr
    assert weak.exit_code == 1
    assert PASSWORD not in created.output
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = (await connection.execute(select(User.email, User.role))).all()
    await engine.dispose()
    assert [tuple(row) for row in rows] == [("root@example.org", UserRole.ADMIN)]


async def _invoke(args: list[str], stdin: str) -> Result:
    get_settings.cache_clear()
    # The command runs its own event loop.
    return await asyncio.to_thread(runner.invoke, cli, args, input=stdin)


@pytest.mark.db
async def test_reset_password_restores_emergency_access(
    monkeypatch: pytest.MonkeyPatch, scratch_database: str
) -> None:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", scratch_database)
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", generate_key())
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    async with engine.begin() as connection:
        user_id = uuid7()
        await connection.execute(
            insert(User).values(
                id=user_id,
                email="sso-admin@example.org",
                display_name="SSO Admin",
                role=UserRole.ADMIN,
                language="en",
                timezone="UTC",
                is_active=True,
            )
        )
        # Signs in via SSO only, local login switched off.
        await connection.execute(
            insert(Identity).values(id=uuid7(), user_id=user_id, provider="oidc:x", subject="1")
        )
        await connection.execute(
            insert(AuthPolicy).values(
                id=uuid7(),
                local_login_enabled=False,
                role_mapping_enabled=False,
                default_role="user",
            )
        )

    unknown = await _invoke(
        ["reset-password", "--email", "nobody@example.org"], f"{PASSWORD}\n{PASSWORD}\n"
    )
    reset = await _invoke(
        ["reset-password", "--email", "SSO-Admin@example.org"], f"{PASSWORD}\n{PASSWORD}\n"
    )

    assert unknown.exit_code == 1
    assert reset.exit_code == 0, reset.output
    assert "switched on again" in reset.stdout
    assert PASSWORD not in reset.output
    async with engine.connect() as connection:
        enabled = await connection.scalar(select(AuthPolicy.local_login_enabled))
        password_hash = await connection.scalar(
            select(Identity.password_hash).where(Identity.provider == "local")
        )
        actions = (await connection.scalars(select(audit_events.c.action))).all()
    await engine.dispose()
    assert enabled is True
    assert password_hash is not None and await verify_password(password_hash, PASSWORD)
    assert actions == ["user.password_set", "idp.config_changed"]

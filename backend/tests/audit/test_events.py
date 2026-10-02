"""One test per audit event type that existing code emits (issue #35).

Event types of features that do not exist yet (role changes, IdP and AI settings,
mailbox creation/sharing, export) are covered by the modules that add them.
"""

import asyncio
import json

import pytest
from click.testing import Result
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from typer.testing import CliRunner

from app import audit
from app.audit import AuditAction
from app.audit.models import audit_events
from app.audit.service import verify_chain
from app.auth.setup import setup_token
from app.cli import cli
from app.core.config import Settings, get_settings
from app.core.crypto import generate_key, set_keyring
from app.mail.models import Mailbox, MailboxType
from app.mail.service import delete_mailbox
from app.mail.storage import AttachmentStorage
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import PASSWORD, login, make_local_user
from tests.factories import make_user

pytestmark = pytest.mark.db

EMAIL = "erika@example.org"


def assert_no_personal_data(row: object) -> None:
    serialized = json.dumps(row._mapping["details"])  # type: ignore[attr-defined]
    assert "@" not in serialized
    assert PASSWORD not in serialized


async def test_setup(db_client: AsyncClient, db_session: AsyncSession, settings: Settings) -> None:
    response = await db_client.post(
        "/setup",
        json={
            "setup_token": setup_token(settings),
            "email": "admin@example.org",
            "display_name": "Admin",
            "password": PASSWORD,
        },
    )

    assert response.status_code == 201
    (row,) = await audit_rows(db_session, AuditAction.SETUP_COMPLETED)
    assert str(row.actor_id) == response.json()["id"]


async def test_login_succeeded(db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_local_user(db_session, EMAIL)

    assert (await login(db_client, EMAIL)).status_code == 200

    (row,) = await audit_rows(db_session, AuditAction.LOGIN_SUCCEEDED)
    assert row.actor_kind == "user"
    assert row.actor_id == user.id
    assert row.details == {"provider": "local"}


async def test_login_failed_wrong_password_and_unknown_user(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_local_user(db_session, EMAIL)

    assert (await login(db_client, EMAIL, "wrong password!")).status_code == 401
    assert (await login(db_client, "nobody@example.org")).status_code == 401

    rows = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert len(rows) == 2
    for row in rows:
        # Anonymous, without the attempted address (no enumeration, no addresses).
        assert row.actor_kind == "anonymous"
        assert row.actor_id is None
        assert row.target_id is None
        assert row.details == {"provider": "local", "reason": "invalid_credentials"}
        assert_no_personal_data(row)


async def test_login_failed_locked(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    await make_local_user(db_session, EMAIL)
    for _ in range(settings.auth.login_max_attempts):
        await login(db_client, EMAIL, "wrong password!")

    assert (await login(db_client, EMAIL)).status_code == 429

    rows = await audit_rows(db_session, AuditAction.LOGIN_FAILED)
    assert rows[-1].details == {"provider": "local", "reason": "locked"}


async def test_logout(db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_local_user(db_session, EMAIL)
    await login(db_client, EMAIL)

    assert (await db_client.post("/auth/logout")).status_code == 204
    # Without a session nothing is recorded.
    assert (await db_client.post("/auth/logout")).status_code == 204

    (row,) = await audit_rows(db_session, AuditAction.LOGOUT)
    assert row.actor_id == user.id


async def test_session_revoked(db_client: AsyncClient, db_session: AsyncSession) -> None:
    user = await make_local_user(db_session, EMAIL)
    await login(db_client, EMAIL)
    sessions = (await db_client.get("/auth/sessions")).json()

    assert (await db_client.delete("/auth/sessions")).status_code == 204
    assert (await db_client.delete(f"/auth/sessions/{sessions[0]['id']}")).status_code == 204

    all_others, single = await audit_rows(db_session, AuditAction.SESSION_REVOKED)
    assert all_others.actor_id == user.id
    assert all_others.target_type == "user"
    assert all_others.details == {"count": 0, "current": False}
    assert single.target_type == "session"
    assert single.target_id == sessions[0]["id"]
    assert single.details == {"count": 1, "current": True}


async def test_user_created_by_admin_and_registration(
    db_client: AsyncClient, db_session: AsyncSession, settings: Settings
) -> None:
    admin = await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    await login(db_client, admin.email)
    created = await db_client.post(
        "/users",
        json={"email": "max@example.org", "display_name": "Max", "password": PASSWORD},
    )
    await db_client.post("/auth/logout")
    settings.auth.local_registration = True
    registered = await db_client.post(
        "/auth/register",
        json={"email": "eva@example.org", "display_name": "Eva", "password": PASSWORD},
    )

    assert created.status_code == registered.status_code == 201
    by_admin, by_registration = await audit_rows(db_session, AuditAction.USER_CREATED)
    assert by_admin.actor_id == admin.id
    assert by_admin.target_id == created.json()["id"]
    assert by_admin.details == {"role": "user", "via": "admin"}
    assert str(by_registration.actor_id) == registered.json()["id"]
    assert by_registration.details == {"role": "user", "via": "registration"}


async def test_mailbox_deleted(db_session: AsyncSession, storage: AttachmentStorage) -> None:
    owner = await make_user(db_session)
    mailbox = Mailbox(
        type=MailboxType.IMAP, display_name="Work", address=EMAIL, owner_user_id=owner.id
    )
    db_session.add(mailbox)
    await db_session.flush()

    assert await delete_mailbox(db_session, mailbox.id, storage, audit.Actor.user(owner.id))
    # Deleting a mailbox that no longer exists records nothing.
    assert not await delete_mailbox(db_session, mailbox.id, storage)

    (row,) = await audit_rows(db_session, AuditAction.MAILBOX_DELETED)
    assert row.actor_id == owner.id
    assert row.target_type == "mailbox"
    assert row.target_id == str(mailbox.id)
    assert_no_personal_data(row)


runner = CliRunner()


async def _invoke(args: list[str], stdin: str | None = None) -> Result:
    get_settings.cache_clear()
    set_keyring(None)
    return await asyncio.to_thread(runner.invoke, cli, args, input=stdin)


async def _scratch_rows(url: str) -> list[object]:
    engine = create_async_engine(url, poolclass=NullPool)
    async with AsyncSession(engine) as db:
        rows = (await db.execute(audit_events.select().order_by(audit_events.c.id))).all()
        assert (await verify_chain(db)).valid
    await engine.dispose()
    return list(rows)


async def test_cli_key_rotation_and_admin_creation(
    monkeypatch: pytest.MonkeyPatch, scratch_database: str
) -> None:
    monkeypatch.setenv("OLLAMAIL_DATABASE_URL", scratch_database)
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", generate_key())

    rotated = await _invoke(["rotate-keys"])
    created = await _invoke(
        ["create-admin", "--email", "root@example.org", "--display-name", "Root"],
        f"{PASSWORD}\n{PASSWORD}\n",
    )

    assert rotated.exit_code == 0, rotated.output
    assert created.exit_code == 0, created.output
    rotation, admin = await _scratch_rows(scratch_database)
    assert rotation.action == AuditAction.KEYS_ROTATED  # type: ignore[attr-defined]
    assert rotation.actor_kind == "system"  # type: ignore[attr-defined]
    assert set(rotation.details) == {"tables", "checked", "rotated"}  # type: ignore[attr-defined]
    assert admin.action == AuditAction.USER_CREATED  # type: ignore[attr-defined]
    assert admin.details == {"role": "admin", "via": "cli"}  # type: ignore[attr-defined]
    assert admin.prev_hash == rotation.hash  # type: ignore[attr-defined]

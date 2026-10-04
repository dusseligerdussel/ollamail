"""Deleting a user removes every row and file derived from them (docs/PRIVACY.md, Art. 17).

The tables are discovered from the schema (``foreign_keys.py``): every table that
references ``users`` directly or through other tables must cascade, and the integration
test fills each of them for the deleted user, so a new module without test data or without
``ON DELETE CASCADE`` makes it fail.
"""

import json
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.core.config import PrivacySettings
from app.digest.storage import DigestStorage
from app.mail import deletion as mail_deletion
from app.mail.storage import AttachmentStorage
from app.privacy import exports
from app.privacy.storage import ExportStorage
from app.users.models import User, UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client
from tests.factories import make_user
from tests.privacy.conftest import (
    FakeServer,
    current_user_id,
    finish_user_deletion,
    seed_user_data,
)
from tests.privacy.foreign_keys import (
    blocking_keys,
    cascading_tables,
    dependent_tables,
    foreign_keys,
    row_counts,
    user_columns,
)

pytestmark = pytest.mark.db

USERS = "users"
# Columns with a user ID that are deliberately no foreign key.
UNLINKED_USER_COLUMNS = {
    # Pseudonymous actor reference; the audit log outlives the user (docs/PRIVACY.md).
    ("audit_events", "actor_id"),
}


async def test_every_table_referencing_a_user_is_deleted_with_it(
    db_session: AsyncSession,
) -> None:
    keys = await foreign_keys(db_session)
    dependent = dependent_tables(keys, USERS)

    # Sanity check: the discovery sees the known modules.
    assert {
        "auth_identities",
        "auth_sessions",
        "mail_mailboxes",
        "mail_messages",
        "search_embeddings",
        "todos",
        "triage_categories",
        "digests",
        "rag_citations",
        "privacy_exports",
    } <= dependent
    assert dependent - cascading_tables(keys, USERS) == set()
    assert blocking_keys(keys, USERS) == []
    # A user ID stored without a foreign key would survive the deletion.
    linked = {(child, column) for child, column, parent, _ in keys if parent == USERS}
    assert (await user_columns(db_session)) - linked - UNLINKED_USER_COLUMNS == set()


def test_discovery_flags_rows_that_would_stay_behind() -> None:
    keys = [
        ("todos", "user_id", USERS, "c"),
        ("notes", "todo_id", "todos", "n"),
        ("labels", "note_id", "notes", "c"),
        ("reports", "user_id", USERS, "a"),
        ("other", "x_id", "unrelated", "c"),
    ]

    assert dependent_tables(keys, USERS) == {"todos", "notes", "labels", "reports"}
    assert cascading_tables(keys, USERS) == {"todos"}
    assert blocking_keys(keys, USERS) == [("reports", "user_id", USERS, "a")]


async def test_deleting_an_account_leaves_no_rows_and_no_files(
    app: FastAPI,
    bob: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
    user_deletion_requests: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependent = dependent_tables(await foreign_keys(db_session), USERS)
    # Another user with data in every module must keep all of it.
    other = await seed_user_data(
        db_session, bob, server, storage, data_dir, address="bob@example.org", marker="bob"
    )
    baseline = await row_counts(db_session, dependent | {USERS})

    await make_local_user(db_session, "erika@example.org")
    erika = api_client(app)
    assert (await login(erika, "erika@example.org")).status_code == 200
    seeded = await seed_user_data(
        db_session, erika, server, storage, data_dir, address="erika@example.org", marker="erika"
    )
    # An export with its file.
    export_storage = ExportStorage(data_dir)
    item, _ = await exports.request_export(db_session, seeded.user_id, expiry_hours=24)
    await db_session.commit()
    await exports.run_export(
        db_session,
        item.id,
        storage=export_storage,
        digest_storage=DigestStorage(data_dir),
        expiry_hours=24,
    )
    export_file = export_storage.path(seeded.user_id, item.id)
    assert export_file.is_file()

    filled = await row_counts(db_session, dependent | {USERS})
    missing = sorted(table for table in dependent if filled[table] <= baseline[table])
    # Every table with user data has data of the deleted user (extend seed_user_data).
    assert missing == []
    assert seeded.attachment_paths and all(storage.exists(p) for p in seeded.attachment_paths)
    assert all(path.is_file() for path in seeded.audio_paths)

    response = await erika.request(
        "DELETE", "/privacy/account", json={"confirm_email": " Erika@Example.org "}
    )

    assert response.status_code == 204
    assert "ollamail_session=" in response.headers.get("set-cookie", "")
    # Signed out everywhere, gone for everybody; the rest runs in the background (#177).
    assert (await erika.get("/auth/me")).status_code == 401
    await erika.aclose()
    assert user_deletion_requests == [seeded.user_id]
    db_session.expunge_all()
    marked = await db_session.get(User, seeded.user_id)
    assert marked is not None and marked.deletion_requested_at is not None
    assert not marked.is_active
    assert "erika" not in f"{marked.email} {marked.display_name}".lower()

    monkeypatch.setattr(mail_deletion, "MESSAGE_BATCH", 2)
    await finish_user_deletion(db_session, seeded.user_id, storage, data_dir)

    db_session.expunge_all()
    assert await row_counts(db_session, dependent | {USERS}) == baseline
    assert await db_session.get(User, seeded.user_id) is None
    # Files: attachments, digest audio, export.
    assert not any(storage.exists(path) for path in seeded.attachment_paths)
    assert not storage.mailbox_dir(seeded.mailbox_id).exists()
    assert not (DigestStorage(data_dir).base / str(seeded.user_id)).exists()
    assert not export_file.exists()
    assert not (export_storage.base / str(seeded.user_id)).exists()
    # The other user is untouched.
    assert all(storage.exists(path) for path in other.attachment_paths)
    assert all(path.is_file() for path in other.audio_paths)
    assert (await bob.get(f"/mailboxes/{other.mailbox_id}")).status_code == 200

    # Provable, without personal data: IDs and counts only.
    [entry] = await audit_rows(db_session, AuditAction.USER_DELETED)
    assert (entry.actor_kind, entry.actor_id) == ("user", seeded.user_id)
    assert (entry.target_type, entry.target_id) == ("user", str(seeded.user_id))
    assert entry.details == {"via": "self", "mailboxes": 1}
    everything = json.dumps([row.details for row in await audit_rows(db_session)])
    assert "erika" not in everything.lower()


async def test_account_deletion_needs_the_matching_address(
    erika: AsyncClient, db_session: AsyncSession
) -> None:
    user_id = await current_user_id(erika)

    response = await erika.request(
        "DELETE", "/privacy/account", json={"confirm_email": "bob@example.org"}
    )

    assert response.status_code == 422
    assert response.json()["type"] == "urn:ollamail:problem:confirmation-mismatch"
    assert "bob@example.org" not in response.text
    assert await db_session.get(User, user_id) is not None


async def test_self_deletion_can_be_disabled(
    app: FastAPI, erika: AsyncClient, db_session: AsyncSession
) -> None:
    app.state.settings.privacy = PrivacySettings(self_delete_enabled=False)

    assert (await erika.get("/privacy/account")).json() == {
        "self_delete_enabled": False,
        "export_expiry_hours": 24,
    }
    response = await erika.request(
        "DELETE", "/privacy/account", json={"confirm_email": "erika@example.org"}
    )

    assert response.status_code == 403
    assert await db_session.get(User, await current_user_id(erika)) is not None


async def test_the_last_admin_cannot_delete_their_account(
    app: FastAPI, db_session: AsyncSession
) -> None:
    admin = await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    async with api_client(app) as client:
        assert (await login(client, "admin@example.org")).status_code == 200
        response = await client.request(
            "DELETE", "/privacy/account", json={"confirm_email": "admin@example.org"}
        )

        assert response.status_code == 409
        assert response.json()["type"] == "urn:ollamail:problem:last-admin"
        assert await db_session.get(User, admin.id) is not None

        # With a second active admin it works.
        await make_local_user(db_session, "second-admin@example.org", role=UserRole.ADMIN)
        response = await client.request(
            "DELETE", "/privacy/account", json={"confirm_email": "admin@example.org"}
        )
        assert response.status_code == 204


async def test_admin_deletes_a_user(
    app: FastAPI,
    erika: AsyncClient,
    bob: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
    user_deletion_requests: list[uuid.UUID],
) -> None:
    seeded = await seed_user_data(
        db_session, bob, server, storage, data_dir, address="bob@example.org", marker="bob"
    )
    admin = await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    async with api_client(app) as client:
        assert (await login(client, "admin@example.org")).status_code == 200

        # Users cannot delete others.
        assert (await erika.delete(f"/admin/privacy/users/{seeded.user_id}")).status_code == 403
        assert (await client.delete(f"/admin/privacy/users/{uuid.uuid4()}")).status_code == 404
        # The only admin cannot delete themselves.
        assert (await client.delete(f"/admin/privacy/users/{admin.id}")).status_code == 409

        response = await client.delete(f"/admin/privacy/users/{seeded.user_id}")

    assert response.status_code == 200
    assert response.json() == {"user_id": str(seeded.user_id), "deleted": True, "mailboxes": 1}
    assert user_deletion_requests == [seeded.user_id]
    await finish_user_deletion(db_session, seeded.user_id, storage, data_dir)
    db_session.expunge_all()
    assert await db_session.get(User, seeded.user_id) is None
    assert not storage.mailbox_dir(seeded.mailbox_id).exists()
    [entry] = await audit_rows(db_session, AuditAction.USER_DELETED)
    assert (entry.actor_id, entry.target_id) == (admin.id, str(seeded.user_id))
    assert entry.details == {"via": "admin", "mailboxes": 1}
    remaining = await db_session.scalars(select(User.email).order_by(User.email))
    assert list(remaining) == ["admin@example.org", "erika@example.org"]


async def test_deletion_keeps_an_admin_who_can_sign_in(
    app: FastAPI, db_session: AsyncSession
) -> None:
    admin_id = (await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)).id
    # Another active admin, but without any identity: nobody could sign in as admin.
    await make_user(db_session, role=UserRole.ADMIN)
    await db_session.commit()
    async with api_client(app) as client:
        assert (await login(client, "admin@example.org")).status_code == 200
        response = await client.request(
            "DELETE", "/privacy/account", json={"confirm_email": "admin@example.org"}
        )

    assert response.status_code == 409
    assert response.json()["type"] == "urn:ollamail:problem:admin-lockout"
    db_session.expunge_all()
    assert await db_session.get(User, admin_id) is not None

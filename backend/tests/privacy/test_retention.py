"""Retention: admin settings, the daily job and the audit log exception."""

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.audit.models import audit_events
from app.audit.service import verify_chain
from app.core.config import Settings
from app.mail.models import Attachment, Message, Thread
from app.mail.storage import AttachmentStorage
from app.privacy import retention
from app.privacy.models import RetentionSettingsRecord
from app.privacy.policy import RetentionPolicy, default_policy, effective_policy, merge
from app.search.models import SearchChunk, SearchEmbedding
from app.users.models import UserRole
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client
from tests.privacy.conftest import NOW, FakeServer, seed_user_data

KEEP = RetentionPolicy(
    mail_days=0,
    attachment_days=0,
    search_index_days=0,
    rag_history_days=0,
    digest_days=30,
    audit_days=0,
)


def policy(**changes: int) -> RetentionPolicy:
    return RetentionPolicy(**{**KEEP.as_dict(), **changes})


def test_admin_values_override_the_environment(settings: Settings) -> None:
    defaults = default_policy(settings)
    assert defaults == RetentionPolicy(
        mail_days=0,
        attachment_days=0,
        search_index_days=0,
        rag_history_days=90,
        digest_days=30,
        audit_days=365,
    )

    record = RetentionSettingsRecord(mail_days=400, audit_days=None, digest_days=7)

    assert merge(defaults, None) == defaults
    assert merge(defaults, record) == RetentionPolicy(
        mail_days=400,
        attachment_days=0,
        search_index_days=0,
        rag_history_days=90,
        digest_days=7,
        audit_days=365,
    )


@pytest.mark.db
async def test_admin_api(app: FastAPI, erika: AsyncClient, db_session: AsyncSession) -> None:
    assert (await erika.get("/admin/privacy/retention")).status_code == 403
    assert (await erika.patch("/admin/privacy/retention", json={})).status_code == 403
    admin = await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    async with api_client(app) as client:
        assert (await login(client, "admin@example.org")).status_code == 200

        response = await client.get("/admin/privacy/retention")
        assert response.status_code == 200
        body = response.json()
        assert body["values"] == body["defaults"]
        assert body["overridden"] == []
        assert body["initial_sync_days"] == 90
        assert body["last_run"] is None

        response = await client.patch(
            "/admin/privacy/retention", json={"mail_days": 365, "audit_days": 730}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["values"]["mail_days"] == 365
        assert body["values"]["audit_days"] == 730
        assert body["defaults"]["audit_days"] == 365
        assert sorted(body["overridden"]) == ["audit_days", "mail_days"]
        assert (await effective_policy(db_session, app.state.settings)).mail_days == 365

        # null resets to the environment default.
        response = await client.patch("/admin/privacy/retention", json={"audit_days": None})
        assert response.json()["values"]["audit_days"] == 365
        assert response.json()["overridden"] == ["mail_days"]

        assert (
            await client.patch("/admin/privacy/retention", json={"digest_days": 0})
        ).status_code == 422
        assert (
            await client.patch("/admin/privacy/retention", json={"unknown": 1})
        ).status_code == 422

    entries = await audit_rows(db_session, audit.AuditAction.RETENTION_CHANGED)
    assert [(e.actor_id, e.details) for e in entries] == [
        (admin.id, {"mail_days": 365, "audit_days": 730}),
        (admin.id, {"audit_days": 365}),
    ]


async def _age(session: AsyncSession, mailbox_id: uuid.UUID, days: int) -> list[uuid.UUID]:
    """Make the mails with attachments of a mailbox ``days`` old; returns their IDs."""
    ids = list(
        await session.scalars(
            select(Message.id).where(Message.mailbox_id == mailbox_id, Message.has_attachments)
        )
    )
    await session.execute(
        update(Message).where(Message.id.in_(ids)).values(received_at=NOW - timedelta(days=days))
    )
    await session.execute(
        update(Thread)
        .where(Thread.id.in_(select(Message.thread_id).where(Message.id.in_(ids))))
        .values(last_message_at=NOW - timedelta(days=days))
    )
    await session.commit()
    return ids


async def _count(session: AsyncSession, model: type, *where: object) -> int:
    return (
        await session.scalar(select(func.count()).select_from(model).where(*where))  # type: ignore[arg-type]
    ) or 0


@pytest.mark.db
async def test_old_mails_are_deleted_with_files(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
) -> None:
    seeded = await seed_user_data(
        db_session, erika, server, storage, data_dir, address="erika@example.org", marker="e"
    )
    old = await _age(db_session, seeded.mailbox_id, days=400)
    total = await _count(db_session, Message, Message.mailbox_id == seeded.mailbox_id)
    paths = list(
        await db_session.scalars(
            select(Attachment.storage_path).where(Attachment.message_id.in_(old))
        )
    )
    assert old and paths and total > len(old)

    result = await retention.enforce(db_session, policy(mail_days=365), storage, now=NOW)

    assert result.mails == len(old)
    assert result.threads >= 1
    assert await _count(db_session, Message, Message.id.in_(old)) == 0
    assert await _count(db_session, Message, Message.mailbox_id == seeded.mailbox_id) == (
        total - len(old)
    )
    assert not any(storage.exists(path) for path in paths)
    # Derived data went with the mails (cascade).
    assert await _count(db_session, SearchChunk, SearchChunk.message_id.in_(old)) == 0
    [entry] = await audit_rows(db_session, audit.AuditAction.DATA_DELETED)
    assert entry.actor_kind == "system"
    assert entry.details["reason"] == "retention"
    assert entry.details["mails"] == len(old)
    record = await db_session.scalar(select(RetentionSettingsRecord))
    assert record is not None and record.last_run_at == NOW
    assert record.last_run["mails"] == len(old)

    # Idempotent: nothing left to delete, no further audit entry.
    again = await retention.enforce(db_session, policy(mail_days=365), storage, now=NOW)
    assert not again.any()
    assert len(await audit_rows(db_session, audit.AuditAction.DATA_DELETED)) == 1


@pytest.mark.db
async def test_attachments_and_search_index_have_their_own_periods(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
) -> None:
    seeded = await seed_user_data(
        db_session, erika, server, storage, data_dir, address="erika@example.org", marker="e"
    )
    old = await _age(db_session, seeded.mailbox_id, days=100)
    paths = list(
        await db_session.scalars(
            select(Attachment.storage_path).where(Attachment.message_id.in_(old))
        )
    )

    # Shorter than the age of no mail: nothing happens.
    assert not (
        await retention.enforce(
            db_session, policy(attachment_days=200, search_index_days=200), storage, now=NOW
        )
    ).any()

    index = await retention.enforce(db_session, policy(search_index_days=30), storage, now=NOW)

    assert index.search_chunks >= 1
    assert (index.mails, index.attachments) == (0, 0)
    assert await _count(db_session, SearchChunk, SearchChunk.message_id.in_(old)) == 0
    assert await _count(db_session, SearchEmbedding) == 0
    assert all(storage.exists(path) for path in paths)

    files = await retention.enforce(db_session, policy(attachment_days=90), storage, now=NOW)

    assert files.attachments == len(paths)
    assert files.mails == 0
    assert await _count(db_session, Message, Message.id.in_(old)) == len(old)
    assert await _count(db_session, Attachment, Attachment.message_id.in_(old)) == 0
    assert not any(storage.exists(path) for path in paths)


async def _add_events(session: AsyncSession, count: int) -> None:
    for number in range(count):
        await audit.record(
            session, audit.SYSTEM, audit.AuditAction.KEYS_ROTATED, None, {"n": number}
        )
    await session.commit()


@pytest.mark.db
async def test_audit_retention_keeps_the_chain_verifiable(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    await _add_events(db_session, 5)
    ids = [row.id for row in await audit_rows(db_session)]
    # Events older than the cutoff: the first three (no UPDATE possible, so a cutoff
    # between their timestamps stands in for "older than N days").
    cutoff_row = (await audit_rows(db_session))[3]

    deleted = await retention.purge_audit(db_session, cutoff_row.occurred_at)
    await db_session.commit()

    assert deleted == 3
    remaining = await audit_rows(db_session)
    assert [row.id for row in remaining] == ids[3:]
    check = await verify_chain(db_session)
    assert check.valid and check.checked == 2

    # The job documents the new starting point in its own (chained) entry.
    now = remaining[-1].occurred_at + timedelta(days=10)
    result = await retention.enforce(db_session, policy(audit_days=5), storage, now=now)
    assert result.audit_events == 1  # the newest event is always kept
    [entry] = await audit_rows(db_session, audit.AuditAction.DATA_DELETED)
    start = (await audit_rows(db_session))[0]
    assert entry.details["chain_start_id"] == start.id
    assert entry.details["chain_start_prev_hash"] == start.prev_hash.hex()
    check = await verify_chain(db_session)
    assert check.valid and check.checked == 2


@pytest.mark.db
async def test_audit_log_stays_append_only_outside_the_purge(db_session: AsyncSession) -> None:
    await _add_events(db_session, 2)

    for statement in (
        "DELETE FROM audit_events",
        "UPDATE audit_events SET action = 'x'",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with db_session.begin_nested():
                await db_session.execute(text(statement))
    assert await db_session.scalar(select(func.count()).select_from(audit_events)) == 2

    # The purge's permission ends with the purge.
    await retention.purge_audit(db_session, NOW + timedelta(days=3650))
    with pytest.raises(DBAPIError, match="append-only"):
        async with db_session.begin_nested():
            await db_session.execute(text("DELETE FROM audit_events"))

"""Deleting a user with mailboxes in the background (#177): what is gone with the request,
the batches of the jobs, resuming after an abort and that nothing stays behind.

The generic check over all tables (every row of the user goes) is
``tests/privacy/test_user_deletion.py``. All names and addresses are invented.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import AuditAction
from app.auth.models import AuthSession, Identity
from app.digest.storage import DigestStorage
from app.mail import deletion as mail_deletion
from app.mail.hooks import MailboxDeleted
from app.mail.models import Attachment, Mailbox, MailboxAssignment, MailboxType, Message, Thread
from app.mail.storage import AttachmentStorage
from app.privacy import deletion, tasks
from app.users.models import User, UserRole
from app.worker import app as worker_app
from tests.audit.conftest import audit_rows
from tests.auth.conftest import login, make_local_user
from tests.conftest import api_client
from tests.factories import make_user
from tests.mail.test_sync_tasks import queue  # noqa: F401
from tests.privacy.conftest import (
    FakeServer,
    add_mailbox,
    current_user_id,
    file_stores,
    finish_user_deletion,
    run_sync,
)

pytestmark = pytest.mark.db

ADDRESSES = ("erika@example.org", "erika.work@example.org", "erika.club@example.org")


async def count(session: AsyncSession, model: type, mailbox_ids: list[uuid.UUID]) -> int:
    column = model.mailbox_id  # type: ignore[attr-defined]
    return (
        await session.scalar(select(func.count()).select_from(model).where(column.in_(mailbox_ids)))
        or 0
    )


async def add_synced_mailboxes(
    session: AsyncSession,
    client: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    addresses: tuple[str, ...],
) -> list[uuid.UUID]:
    ids = []
    for address in addresses:
        mailbox_id = uuid.UUID(str((await add_mailbox(client, address=address))["id"]))
        await run_sync(session, mailbox_id, server, storage)
        ids.append(mailbox_id)
    return ids


@pytest.fixture
async def admin(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)
    async with api_client(app) as client:
        assert (await login(client, "admin@example.org")).status_code == 200
        yield client


def stub_database(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    """Jobs use the test session instead of the worker's pool."""

    class Database:
        @asynccontextmanager
        async def sessionmaker(self) -> AsyncIterator[AsyncSession]:
            yield session

    monkeypatch.setattr(tasks, "get_database", Database)


async def test_large_user_is_gone_with_the_request_and_removed_in_batches(
    erika: AsyncClient,
    bob: AsyncClient,
    admin: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
    user_deletion_requests: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await current_user_id(erika)
    mailbox_ids = await add_synced_mailboxes(db_session, erika, server, storage, ADDRESSES)
    other_ids = await add_synced_mailboxes(db_session, bob, server, storage, ("bob@example.org",))
    shared = Mailbox(
        type=MailboxType.IMAP, display_name="Team", address="team@example.org", is_shared=True
    )
    db_session.add(shared)
    await db_session.flush()
    db_session.add(MailboxAssignment(mailbox_id=shared.id, user_id=user_id))
    await db_session.commit()
    messages = await count(db_session, Message, mailbox_ids)
    paths = list(
        await db_session.scalars(
            select(Attachment.storage_path).where(Attachment.mailbox_id.in_(mailbox_ids))
        )
    )
    assert messages > len(mailbox_ids) * 2 and paths

    response = await admin.delete(f"/admin/privacy/users/{user_id}")

    assert response.status_code == 200
    assert response.json() == {"user_id": str(user_id), "deleted": True, "mailboxes": 3}
    assert user_deletion_requests == [user_id]
    # Nothing large is deleted in the request ...
    assert await count(db_session, Message, mailbox_ids) == messages
    # ... but the user and their data are gone for everybody, admins included.
    assert (await erika.get("/auth/me")).status_code == 401
    assert str(user_id) not in [user["id"] for user in (await admin.get("/users")).json()]
    # Cannot be reactivated either.
    assert (await admin.patch(f"/users/{user_id}", json={"is_active": True})).status_code == 404
    assert (await admin.delete(f"/admin/privacy/users/{user_id}")).status_code == 404
    db_session.expunge_all()
    marked = await db_session.get(User, user_id)
    assert marked is not None and not marked.is_active
    assert "erika" not in f"{marked.email} {marked.display_name}".lower()
    for model in (Identity, AuthSession, MailboxAssignment):
        assert (
            await db_session.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
            == 0
        )
    pending = await db_session.scalars(
        select(Mailbox.id).where(Mailbox.deletion_requested_at.is_not(None))
    )
    assert sorted(pending) == sorted(mailbox_ids)
    # The address is free again at once.
    assert (await make_local_user(db_session, "erika@example.org")).id != user_id
    # Provable: one ``user.deleted`` and one ``mailbox.deleted`` per mailbox.
    [entry] = await audit_rows(db_session, AuditAction.USER_DELETED)
    assert entry.details == {"via": "admin", "mailboxes": 3}
    removed = await audit_rows(db_session, AuditAction.MAILBOX_DELETED)
    assert sorted(row.target_id for row in removed) == sorted(map(str, mailbox_ids))

    monkeypatch.setattr(mail_deletion, "MESSAGE_BATCH", 2)
    monkeypatch.setattr(mail_deletion, "THREAD_BATCH", 1)
    await finish_user_deletion(db_session, user_id, storage, data_dir)

    db_session.expunge_all()
    assert await db_session.get(User, user_id) is None
    assert (
        list(await db_session.scalars(select(Mailbox.id).where(Mailbox.id.in_(mailbox_ids)))) == []
    )
    for model in (Message, Thread, Attachment):
        assert await count(db_session, model, mailbox_ids) == 0, model
    assert not any(storage.exists(path) for path in paths)
    assert not any(storage.mailbox_dir(mailbox_id).exists() for mailbox_id in mailbox_ids)
    # Others are untouched: bob's mailbox, the shared mailbox, the new account.
    assert await count(db_session, Message, other_ids) > 0
    assert (await bob.get(f"/mailboxes/{other_ids[0]}")).status_code == 200
    assert await db_session.get(Mailbox, shared.id) is not None
    assert await db_session.scalar(select(User.id).where(User.email == "erika@example.org"))


async def test_a_user_without_mailboxes_is_deleted_at_once(
    erika: AsyncClient,
    admin: AsyncClient,
    db_session: AsyncSession,
    user_deletion_requests: list[uuid.UUID],
) -> None:
    user_id = await current_user_id(erika)

    response = await admin.delete(f"/admin/privacy/users/{user_id}")

    assert response.json() == {"user_id": str(user_id), "deleted": True, "mailboxes": 0}
    assert user_deletion_requests == []
    db_session.expunge_all()
    assert await db_session.get(User, user_id) is None


async def test_deletion_resumes_after_an_abort_and_leaves_nothing(
    erika: AsyncClient,
    server: FakeServer,
    storage: AttachmentStorage,
    data_dir: Path,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await current_user_id(erika)
    mailbox_ids = await add_synced_mailboxes(db_session, erika, server, storage, ADDRESSES[:2])
    response = await erika.request(
        "DELETE", "/privacy/account", json={"confirm_email": "erika@example.org"}
    )
    assert response.status_code == 204
    stub_database(monkeypatch, db_session)
    deferred_mailboxes: list[uuid.UUID] = []
    deferred_users: list[uuid.UUID] = []

    async def defer_mailbox(mailbox_id: uuid.UUID) -> bool:
        deferred_mailboxes.append(mailbox_id)
        return True

    async def defer_user(user_id: uuid.UUID) -> bool:
        deferred_users.append(user_id)
        return True

    monkeypatch.setattr(tasks.mail_deletion, "defer_deletion", defer_mailbox)
    monkeypatch.setattr(tasks, "defer_user_deletion", defer_user)

    # The first run of ``privacy.delete_user`` queues the mailboxes and waits for them.
    await tasks.delete_user_job(str(user_id))
    assert sorted(deferred_mailboxes) == sorted(mailbox_ids)
    db_session.expunge_all()
    assert await db_session.get(User, user_id) is not None

    # The worker dies in the middle of the first mailbox (after one committed batch).
    async def crash(*_: object) -> None:
        raise RuntimeError("worker lost")

    with monkeypatch.context() as patched:
        patched.setattr(mail_deletion, "MESSAGE_BATCH", 2)
        patched.setattr(mail_deletion, "_delete_files", crash)
        with pytest.raises(RuntimeError):
            await mail_deletion.purge_mailbox(db_session, mailbox_ids[0], storage)
    left = await count(db_session, Message, mailbox_ids[:1])
    assert left > 0

    # The periodic job picks the user up again; no mailbox is left out.
    await tasks.resume_user_deletions(timestamp=0)
    assert deferred_users == [user_id]
    await finish_user_deletion(db_session, user_id, storage, data_dir)

    db_session.expunge_all()
    assert await db_session.get(User, user_id) is None
    for model in (Message, Thread, Attachment):
        assert await count(db_session, model, mailbox_ids) == 0, model
    assert not any(storage.mailbox_dir(mailbox_id).exists() for mailbox_id in mailbox_ids)
    assert await deletion.pending_user_deletions(db_session) == []
    # A late duplicate of the job only cleans up leftover files.
    leftover = DigestStorage(data_dir).base / str(user_id)
    leftover.mkdir(parents=True)
    assert await deletion.purge_user(db_session, user_id, file_stores(data_dir)) == (
        deletion.PurgeResult()
    )
    assert not leftover.exists()


async def test_purge_leaves_users_that_are_not_marked_alone(
    db_session: AsyncSession, data_dir: Path
) -> None:
    user = await make_user(db_session)
    await db_session.commit()

    assert await deletion.purge_user(db_session, user.id, file_stores(data_dir)) == (
        deletion.PurgeResult()
    )
    assert await db_session.get(User, user.id) is not None


async def test_a_removed_mailbox_continues_the_deletion_of_its_owner(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = await make_user(db_session)
    marked.deletion_requested_at = datetime.now(UTC)
    other = await make_user(db_session)
    await db_session.commit()
    stub_database(monkeypatch, db_session)
    deferred: list[uuid.UUID] = []

    async def defer_user(user_id: uuid.UUID) -> bool:
        deferred.append(user_id)
        return True

    monkeypatch.setattr(tasks, "defer_user_deletion", defer_user)

    for owner in (marked.id, other.id, None):
        await tasks.continue_user_deletion(MailboxDeleted(uuid.uuid4(), owner))

    assert deferred == [marked.id]


async def test_defer_queues_one_job_per_user(queue: None) -> None:  # noqa: F811
    first, second = uuid.uuid4(), uuid.uuid4()

    assert await tasks.defer_user_deletion(first) is True
    assert await tasks.defer_user_deletion(first) is False  # one is already waiting
    assert await tasks.defer_user_deletion(second) is True

    jobs = await worker_app.job_manager.list_jobs_async(task=tasks.DELETE_USER_TASK)
    assert sorted(job.task_kwargs["user_id"] for job in jobs) == sorted([str(first), str(second)])
    assert {job.lock for job in jobs} == {f"user_deletion:{first}", f"user_deletion:{second}"}

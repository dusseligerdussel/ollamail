"""Privacy jobs in a real worker, and the admin retention periods in other modules' jobs."""

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.core.config import DatabaseSettings, Settings, StorageSettings, get_settings
from app.core.db import Database
from app.digest import tasks as digest_tasks
from app.digest.models import Digest, DigestLength, DigestStatus, DigestTrigger
from app.digest.storage import DigestStorage
from app.mail.models import Mailbox, MailboxType, Message
from app.privacy import tasks
from app.privacy.models import DataExport, ExportStatus, RetentionSettingsRecord
from app.privacy.storage import ExportStorage
from app.processing.tasks import use_database
from app.worker import TASK_MODULES, app
from tests.auth.conftest import scratch_database as scratch_database
from tests.factories import make_user
from tests.privacy.conftest import utcnow
from tests.processing.conftest import Pipeline
from tests.processing.conftest import pipeline as pipeline


def test_tasks_are_registered() -> None:
    assert "app.privacy.tasks" in TASK_MODULES
    assert app.tasks["privacy.export"].queue == "default"
    periodic = {p.periodic_id: p for p in app.periodic_registry.periodic_tasks.values()}
    assert periodic["privacy_retention"].task is tasks.enforce_retention
    assert periodic["privacy_cleanup_exports"].task is tasks.cleanup_exports


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    settings = Settings(storage=StorageSettings.model_validate({"data_dir": tmp_path}))
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(digest_tasks, "get_storage", lambda: DigestStorage(tmp_path))
    return tmp_path


@pytest.fixture
async def retention_record(pipeline: Pipeline) -> AsyncIterator[None]:
    yield
    async with pipeline.database.sessionmaker() as session:
        await session.execute(delete(RetentionSettingsRecord))
        await session.commit()


@pytest.mark.db
async def test_export_job_builds_the_zip(pipeline: Pipeline, data_dir: Path) -> None:
    async with pipeline.database.sessionmaker() as session:
        item = DataExport(user_id=pipeline.owner_id, status=ExportStatus.PENDING)
        session.add(item)
        await session.commit()
        export_id = item.id

    async with app.open_async():
        await tasks.enqueue_export(export_id)
        await tasks.enqueue_export(export_id)  # still waiting: no second job
    assert await pipeline.pending_jobs() == 1
    await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        done = await session.get(DataExport, export_id)
        assert done is not None and done.status is ExportStatus.READY
        assert done.expires_at is not None and done.expires_at > utcnow() + timedelta(hours=23)
    assert ExportStorage(data_dir).path(pipeline.owner_id, export_id).is_file()


@pytest.mark.db
async def test_digest_cleanup_uses_the_admin_retention(
    pipeline: Pipeline, data_dir: Path, retention_record: None
) -> None:
    async with pipeline.database.sessionmaker() as session:
        digest = Digest(
            user_id=pipeline.owner_id,
            trigger=DigestTrigger.MANUAL,
            status=DigestStatus.READY,
            period_start=utcnow() - timedelta(days=11),
            period_end=utcnow() - timedelta(days=10),
            language="en",
            length=DigestLength.SHORT,
            mailbox_ids=[pipeline.mailbox_id],
        )
        session.add(digest)
        await session.flush()
        digest.created_at = utcnow() - timedelta(days=10)
        await session.commit()
        digest_id = digest.id

    async def exists() -> bool:
        async with pipeline.database.sessionmaker() as session:
            found = await session.scalar(select(Digest.id).where(Digest.id == digest_id))
            return found is not None

    timestamp = int(utcnow().timestamp())
    # Environment default (30 days): kept.
    assert get_settings().digest.retention_days == 30
    await digest_tasks.cleanup_digests(timestamp)
    assert await exists()

    async with pipeline.database.sessionmaker() as session:
        session.add(RetentionSettingsRecord(digest_days=7))
        await session.commit()
    await digest_tasks.cleanup_digests(timestamp)
    assert not await exists()


@pytest.mark.db
async def test_retention_job_runs(scratch_database: str, data_dir: Path) -> None:
    # A scratch database: the job commits an audit entry, which cannot be removed again.
    database = Database(DatabaseSettings.model_validate({"url": scratch_database}))
    try:
        async with database.sessionmaker() as session:
            owner = await make_user(session)
            mailbox = Mailbox(
                type=MailboxType.IMAP,
                display_name="Test",
                address="test@example.org",
                owner_user_id=owner.id,
            )
            session.add(mailbox)
            await session.flush()
            old, new = (
                Message(mailbox_id=mailbox.id, remote_ref=ref, received_at=utcnow() - age)
                for ref, age in (("1", timedelta(days=40)), ("2", timedelta(days=2)))
            )
            session.add_all([old, new, RetentionSettingsRecord(mail_days=30)])
            await session.commit()

        with use_database(database):
            await tasks.enforce_retention(int(utcnow().timestamp()))

        async with database.sessionmaker() as session:
            left = await session.scalars(select(Message.id).where(Message.mailbox_id == mailbox.id))
            assert list(left) == [new.id]
            record = await session.scalar(select(RetentionSettingsRecord))
            assert record is not None and record.last_run["mails"] == 1
    finally:
        await database.dispose()

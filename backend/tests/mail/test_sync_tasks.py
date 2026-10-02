"""Worker integration of the mail sync: job deduplication and the watcher service."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import DatabaseSettings, MailSettings, Settings, WorkerSettings
from app.mail import hooks
from app.mail.sync.tasks import request_flag_write, request_sync
from app.worker import app, background_services, build_connector


async def _execute(url: str, statement: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text(statement))
    await engine.dispose()


@pytest.fixture
async def queue(migrated_database: str) -> AsyncIterator[None]:
    settings = DatabaseSettings.model_validate({"url": migrated_database})
    await _execute(migrated_database, "DELETE FROM procrastinate_jobs")
    with app.replace_connector(build_connector(settings)):
        async with app.open_async():
            yield
    await _execute(migrated_database, "DELETE FROM procrastinate_jobs")


@pytest.mark.db
async def test_request_sync_queues_one_job_per_mailbox(queue: None) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()

    assert await request_sync(first) is True
    assert await request_sync(first) is False  # one is already waiting
    assert await request_sync(second) is True

    jobs = await app.job_manager.list_jobs_async(task="mail.sync_mailbox")
    assert sorted(job.task_kwargs["mailbox_id"] for job in jobs) == sorted(
        [str(first), str(second)]
    )
    assert {job.queue for job in jobs} == {"sync"}
    assert {job.lock for job in jobs} == {f"mailbox:{first}", f"mailbox:{second}"}


@pytest.mark.db
async def test_request_flag_write_serialises_jobs_per_message(queue: None) -> None:
    message_id = uuid.uuid4()
    await request_flag_write(message_id)
    await request_flag_write(message_id)

    jobs = await app.job_manager.list_jobs_async(task="mail.write_flags")
    assert [job.task_kwargs["message_id"] for job in jobs] == [str(message_id)] * 2
    assert {job.lock for job in jobs} == {f"message_flags:{message_id}"}
    assert {job.queue for job in jobs} == {"sync"}


async def test_watcher_runs_only_in_sync_workers() -> None:
    stop = asyncio.Event()
    llm_only = Settings(worker=WorkerSettings(queues=["llm"]))
    disabled = Settings(mail=MailSettings(watch_enabled=False))
    assert background_services(llm_only, stop) == []
    assert background_services(disabled, stop) == []

    stop.set()
    services = background_services(Settings(), stop)
    assert [task.get_name() for task in services] == ["mail-watcher"]
    for task in services:
        task.cancel()
    await asyncio.gather(*services, return_exceptions=True)


async def test_processing_pipeline_handles_stored_messages(
    isolated_hooks: list[hooks.MessageStoredHandler], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.processing import tasks as processing

    assert processing.process_stored_message in isolated_hooks
    calls: list[tuple[uuid.UUID, int]] = []

    async def enqueue(message_id: uuid.UUID, *, priority: int) -> None:
        calls.append((message_id, priority))

    monkeypatch.setattr(processing, "enqueue_processing", enqueue)
    new, old = uuid.uuid4(), uuid.uuid4()
    hooks.on_message_stored(processing.process_stored_message)
    await hooks.message_stored(uuid.uuid4(), new)
    await hooks.message_stored(uuid.uuid4(), old, backfill=True)

    assert calls == [(new, processing.Priority.NEW), (old, processing.Priority.BACKFILL)]

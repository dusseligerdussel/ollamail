"""Job ``ai.pull_model``: progress and failures end up in ``ai_model_pulls``."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import respx
from sqlalchemy import delete

from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.gateway import LLMGateway
from app.ai.settings import pulls
from app.ai.settings.models import AIModelPull
from app.ai.settings.runtime import use_worker_gateway
from app.core.config import DatabaseSettings, LLMSettings
from app.core.db import Database

pytestmark = pytest.mark.db

OLLAMA = "http://ollama.test:11434"


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[Database]:
    database = Database(DatabaseSettings.model_validate({"url": migrated_database}))
    gateway = LLMGateway(EnvConfigResolver(LLMSettings(base_url=OLLAMA)))
    try:
        with use_worker_gateway(gateway):
            yield database
    finally:
        async with database.sessionmaker() as session:
            await session.execute(delete(AIModelPull))
            await session.commit()
        await gateway.aclose()
        await database.dispose()


async def _start(database: Database, model: str = "bge-m3:latest") -> AIModelPull:
    async with database.sessionmaker() as session:
        pull, start = await pulls.start_pull(session, "default", model)
        await session.commit()
    assert start
    return pull


async def _get(database: Database, pull: AIModelPull) -> AIModelPull:
    async with database.sessionmaker() as session:
        row = await session.get(AIModelPull, pull.id)
    assert row is not None
    return row


@respx.mock
async def test_pull_records_progress_and_success(database: Database) -> None:
    lines = [
        {"status": "pulling manifest"},
        {"status": "pulling", "digest": "sha256:a", "total": 1000, "completed": 500},
        {"status": "pulling", "digest": "sha256:a", "total": 1000, "completed": 1000},
        {"status": "success"},
    ]
    respx.post(f"{OLLAMA}/api/pull").respond(
        200, content="\n".join(json.dumps(line) for line in lines).encode()
    )
    pull = await _start(database)

    await pulls.run_pull(database, pull.id)

    row = await _get(database, pull)
    assert (row.status, row.completed, row.total, row.error_code) == ("done", 1000, 1000, None)


@respx.mock
async def test_pull_failure_keeps_only_a_code(database: Database) -> None:
    respx.post(f"{OLLAMA}/api/pull").respond(
        200, content=json.dumps({"error": "file does not exist"}).encode()
    )
    pull = await _start(database)

    await pulls.run_pull(database, pull.id)

    row = await _get(database, pull)
    assert (row.status, row.error_code) == ("failed", "model_not_found")


async def test_finished_or_stale_pulls_can_start_again(database: Database) -> None:
    pull = await _start(database)
    async with database.sessionmaker() as session:
        again, start = await pulls.start_pull(session, "default", "bge-m3:latest")
        assert (again.id, start) == (pull.id, False)
        # A worker died: no progress for longer than STALE_AFTER.
        again.updated_at = datetime.now(UTC) - pulls.STALE_AFTER - timedelta(seconds=1)
        again.status = pulls.PullStatus.RUNNING
        await session.commit()
    async with database.sessionmaker() as session:
        restarted, start = await pulls.start_pull(session, "default", "bge-m3:latest")
        await session.commit()
    assert start
    assert restarted.status == pulls.PullStatus.QUEUED

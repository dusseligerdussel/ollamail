"""Digest jobs in a real worker: schedule -> text (llm queue) -> audio (tts queue)."""

from collections.abc import Iterator
from datetime import time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.llm import CloudLLMDisabledError, LLMUnavailableError
from app.digest import tasks
from app.digest.models import Digest, DigestStatus, DigestUserSettings
from app.digest.storage import DigestStorage
from app.worker import TASK_MODULES, app
from tests.digest.conftest import FakeLLM, FakeTTS, make_llm, make_tts, utc
from tests.processing.conftest import Pipeline
from tests.processing.conftest import pipeline as pipeline

# Friday, 2 October 2026, 07:00 in UTC (the test user's time zone).
SLOT = utc(2026, 10, 2, 7, 0)


def test_tasks_are_registered() -> None:
    assert "app.digest.tasks" in TASK_MODULES
    assert app.tasks["digest.generate"].queue == "llm"
    assert app.tasks["digest.synthesize"].queue == "tts"
    periodic = {p.periodic_id: p for p in app.periodic_registry.periodic_tasks.values()}
    assert periodic["digest_schedule"].task is tasks.schedule_digests
    assert periodic["digest_cleanup"].task is tasks.cleanup_digests


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[FakeLLM, FakeTTS]]:
    llm, tts = make_llm(), make_tts()
    monkeypatch.setattr(tasks, "get_llm", lambda: llm.gateway)
    monkeypatch.setattr(tasks, "get_tts", lambda: tts.service)
    monkeypatch.setattr(tasks, "get_storage", lambda: DigestStorage(tmp_path))
    yield llm, tts


async def _enable(pipeline: Pipeline) -> None:
    async with pipeline.database.sessionmaker() as session:
        session.add(
            DigestUserSettings(
                user_id=pipeline.owner_id,
                enabled=True,
                delivery_time=time(7, 0),
                last_scheduled_for=SLOT - timedelta(days=1),
            )
        )
        await session.commit()


async def _digests(pipeline: Pipeline) -> list[Digest]:
    async with pipeline.database.sessionmaker() as session:
        found = await session.scalars(select(Digest).where(Digest.user_id == pipeline.owner_id))
        return list(found)


@pytest.mark.db
async def test_scheduled_digest_end_to_end(
    pipeline: Pipeline,
    fakes: tuple[FakeLLM, FakeTTS],
    tmp_path: Path,
) -> None:
    llm, tts = fakes
    await _enable(pipeline)
    await pipeline.add_message(received_at=SLOT - timedelta(hours=2))
    await pipeline.add_message(received_at=SLOT + timedelta(minutes=5))  # next digest

    async with app.open_async():
        await tasks.schedule_digests(timestamp=int((SLOT + timedelta(seconds=20)).timestamp()))
        await tasks.schedule_digests(timestamp=int((SLOT + timedelta(seconds=80)).timestamp()))
        # Text job, then the audio job it queues.
        for _ in range(2):
            await app.run_worker_async(
                queues=["llm", "tts"],
                wait=False,
                install_signal_handlers=False,
                listen_notify=False,
            )

    [digest] = await _digests(pipeline)
    assert digest.status is DigestStatus.READY
    assert digest.message_count == 1
    assert llm.provider.kinds == ["map", "reduce"]
    assert tts.engine.calls
    assert (tmp_path / digest.audio["mp3"]["path"]).is_file()


@pytest.mark.db
async def test_failures_are_recorded(
    pipeline: Pipeline,
    fakes: tuple[FakeLLM, FakeTTS],
) -> None:
    llm, _ = fakes
    await _enable(pipeline)
    await pipeline.add_message(received_at=SLOT - timedelta(hours=2))
    async with pipeline.database.sessionmaker() as session:
        from app.core.config import DigestSettings
        from app.digest import service

        [digest_id] = await service.schedule_due(
            session, now=SLOT + timedelta(minutes=1), config=DigestSettings()
        )

    # Temporary error: recorded, raised for a retry.
    llm.provider.answers = []

    async def unavailable(*args: object, **kwargs: object) -> None:
        raise LLMUnavailableError("down")

    llm.provider.complete = unavailable  # type: ignore[assignment,method-assign]
    with pytest.raises(LLMUnavailableError):
        await tasks.generate_digest(digest_id=str(digest_id))
    [digest] = await _digests(pipeline)
    assert (digest.status, digest.error_code) == (DigestStatus.FAILED, "llm_unavailable_error")

    # Cloud LLM switched off by the admin: permanent, no retry.
    async def disabled(*args: object, **kwargs: object) -> None:
        raise CloudLLMDisabledError("cloud")

    llm.provider.complete = disabled  # type: ignore[assignment,method-assign]
    await tasks.generate_digest(digest_id=str(digest_id))
    [digest] = await _digests(pipeline)
    assert (digest.status, digest.error_code) == (DigestStatus.FAILED, "llm_cloud_disabled")

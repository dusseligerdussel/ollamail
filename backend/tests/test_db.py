import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy import String, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.pool import NullPool

from app.core.db import Base, get_db
from app.core.ids import uuid7
from tests.conftest import ExtraRoute


class Probe(Base):
    """Test-only model; its table is created inside the rolled-back test transaction."""

    __tablename__ = "test_probe"

    name: Mapped[str] = mapped_column(String(50))


PROBE_TABLE = Base.metadata.tables["test_probe"]


async def create_probe(session: Annotated[AsyncSession, Depends(get_db)]) -> dict[str, str]:
    probe = Probe(name="via-api")
    session.add(probe)
    await session.commit()
    return {"id": str(probe.id)}


@pytest.fixture
def extra_routes() -> list[ExtraRoute]:
    return [("/probes", create_probe, ["POST"])]


@pytest.fixture
async def probe_session(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    connection = await db_session.connection()
    await connection.run_sync(lambda sync: PROBE_TABLE.create(sync))
    yield db_session


def test_uuid7_version_and_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_is_time_ordered_and_unique() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()

    assert first < second
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_uuid7_embeds_current_time() -> None:
    before = time.time_ns() // 1_000_000
    timestamp = uuid7().int >> 80
    after = time.time_ns() // 1_000_000

    assert before <= timestamp <= after


def test_base_provides_id_and_timestamps() -> None:
    columns = PROBE_TABLE.columns

    assert {"id", "created_at", "updated_at", "name"} <= set(columns.keys())
    assert columns["id"].primary_key
    assert PROBE_TABLE.primary_key.name == "pk_test_probe"
    assert columns["updated_at"].onupdate is not None


@pytest.mark.db
async def test_model_gets_uuid7_and_timestamps(probe_session: AsyncSession) -> None:
    probe = Probe(name="first")
    probe_session.add(probe)
    await probe_session.commit()
    await probe_session.refresh(probe)

    assert probe.id.version == 7
    assert probe.created_at.tzinfo is not None
    assert probe.updated_at == probe.created_at


@pytest.mark.db
async def test_test_transaction_is_rolled_back(
    probe_session: AsyncSession, migrated_database: str
) -> None:
    probe_session.add(Probe(name="invisible"))
    await probe_session.commit()

    engine = create_async_engine(migrated_database, poolclass=NullPool)
    async with engine.connect() as other:
        exists = await other.scalar(select(func.to_regclass("test_probe")))
    await engine.dispose()

    # The table and row only exist inside the test's own transaction.
    assert exists is None


@pytest.mark.db
async def test_get_db_dependency_uses_test_session(
    db_client: AsyncClient, probe_session: AsyncSession
) -> None:
    response = await db_client.post("/probes")

    assert response.status_code == 200
    stored = await probe_session.scalar(select(Probe).where(Probe.name == "via-api"))
    assert stored is not None
    assert str(stored.id) == response.json()["id"]


async def test_process_database_is_shared_and_disposed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import db

    monkeypatch.setattr(db, "_process_database", None)
    first = db.process_database()

    assert db.process_database() is first
    await db.dispose_process_database()
    assert db._process_database is None
    await db.dispose_process_database()  # nothing to do


async def test_bound_process_database_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import db
    from app.core.config import get_settings

    monkeypatch.setattr(db, "_process_database", None)
    bound = db.Database(get_settings().database)

    with db.bind_process_database(bound):
        assert db.process_database() is bound
    assert db._process_database is None
    await bound.dispose()


async def test_worker_tasks_share_one_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs of all modules use the process database instead of engines of their own."""
    from app.ai.settings import runtime
    from app.core import db
    from app.mail.sync import tasks as sync_tasks
    from app.processing import tasks as processing_tasks

    monkeypatch.setattr(db, "_process_database", None)
    monkeypatch.setattr(runtime, "_resolver", None)
    shared = db.process_database()
    try:
        assert processing_tasks.get_database() is shared
        assert sync_tasks._database() is shared
        assert runtime.worker_resolver()._sessionmaker is shared.sessionmaker
    finally:
        await runtime.worker_resolver().aclose()
        monkeypatch.setattr(runtime, "_resolver", None)
        await db.dispose_process_database()

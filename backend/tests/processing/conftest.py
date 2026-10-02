"""Fixtures for pipeline tests: dummy steps, committed test data and a worker that drains
the queues. The worker and the jobs commit, so the fixtures clean up after themselves.
All data is synthetic."""

import importlib
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from procrastinate import RetryStrategy
from procrastinate.periodic import PeriodicRegistry
from sqlalchemy import delete, text

from app.core.config import DatabaseSettings, QueueName
from app.core.db import Database
from app.core.ids import uuid7
from app.mail.models import Mailbox, MailboxType, Message
from app.processing.steps import ProcessingStep, StepContext, registry
from app.processing.tasks import use_database
from app.users.models import User
from app.worker import TASK_MODULES, app, build_connector
from tests.factories import make_user

# Retries without waiting, so tests stay fast.
FAST_RETRY = RetryStrategy(max_attempts=2)
QUEUES = ["default", "llm", "sync"]

# Feature modules register their steps on import. Import them now: the worker imports
# them lazily, which would register them in a test's isolated registry instead.
for _module in TASK_MODULES:
    importlib.import_module(_module)


@dataclass
class Recorder:
    """Records which step ran for which message, in order."""

    calls: list[tuple[str, uuid.UUID]] = field(default_factory=list)
    # Remaining failures per step name.
    failures: dict[str, int] = field(default_factory=dict)

    def step(
        self,
        name: str,
        *,
        version: int = 1,
        depends_on: tuple[str, ...] = (),
        queue: QueueName = "default",
        error: Exception | None = None,
    ) -> ProcessingStep:
        async def handler(ctx: StepContext) -> None:
            self.calls.append((name, ctx.message_id))
            if self.failures.get(name, 0) > 0:
                self.failures[name] -= 1
                raise error or RuntimeError("Re: Quarterly numbers from alice@example.org")

        return ProcessingStep(
            name=name,
            version=version,
            queue=queue,
            handler=handler,
            depends_on=depends_on,
            retry=FAST_RETRY,
        )

    def names(self, message_id: uuid.UUID | None = None) -> list[str]:
        return [name for name, mid in self.calls if message_id is None or mid == message_id]


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    # Feature modules register their steps on import; the worker imports them while the
    # test runs. Import them now so they land in the real registry, not the isolated one.
    for module in TASK_MODULES:
        importlib.import_module(module)
    with registry.isolated():
        yield Recorder()


async def run_worker(queues: Iterable[str] = QUEUES, concurrency: int = 1) -> None:
    """Run a worker until its queues are empty, without its periodic deferrer.

    A real worker also defers every periodic task that came due in the last ten minutes
    (``digest.schedule`` and ``triage.write_back`` every minute, for example). Those jobs
    belong to no test: depending on the clock they would land in the middle of a test,
    run against its data or stay queued (retries with a wait, queues the test does not
    drain). Tests call periodic tasks directly instead."""
    registry = app.periodic_registry
    app.periodic_registry = PeriodicRegistry()
    try:
        await app.run_worker_async(
            queues=list(queues),
            concurrency=concurrency,
            wait=False,
            install_signal_handlers=False,
            listen_notify=False,
        )
    finally:
        app.periodic_registry = registry


@dataclass
class Pipeline:
    database: Database
    settings: DatabaseSettings
    mailbox_id: uuid.UUID
    owner_id: uuid.UUID

    async def add_message(self, received_at: datetime | None = None) -> uuid.UUID:
        message_id = uuid7()
        async with self.database.sessionmaker() as session:
            session.add(
                Message(
                    id=message_id,
                    mailbox_id=self.mailbox_id,
                    remote_ref=str(message_id),
                    received_at=received_at,
                )
            )
            await session.commit()
        return message_id

    async def execute(self, statement: str, **params: object) -> list[tuple[object, ...]]:
        async with self.database.engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return [tuple(row) for row in result] if result.returns_rows else []

    async def pending_jobs(self) -> int:
        rows = await self.execute(
            "SELECT count(*) FROM procrastinate_jobs WHERE status IN ('todo', 'doing')"
        )
        return int(str(rows[0][0]))

    async def drain(self, concurrency: int = 1) -> None:
        """Run a worker until no job is left (retries without wait included)."""
        async with app.open_async():
            for _ in range(50):
                await run_worker(concurrency=concurrency)
                if await self.pending_jobs() == 0:
                    return
        raise AssertionError("jobs left after draining")


@pytest.fixture
async def pipeline(migrated_database: str) -> AsyncIterator[Pipeline]:
    settings = DatabaseSettings.model_validate({"url": migrated_database})
    database = Database(settings)
    mailbox_id = uuid7()
    async with database.sessionmaker() as session:
        await session.execute(text("DELETE FROM procrastinate_jobs"))
        owner_id = (await make_user(session)).id
        session.add(
            Mailbox(
                id=mailbox_id,
                type=MailboxType.IMAP,
                display_name="Test",
                address="test@example.org",
                owner_user_id=owner_id,
            )
        )
        await session.commit()
    try:
        with app.replace_connector(build_connector(settings)), use_database(database):
            yield Pipeline(database, settings, mailbox_id, owner_id)
    finally:
        async with database.sessionmaker() as session:
            # Cascades to the mailbox; committed users would mark the instance as set up.
            await session.execute(delete(User).where(User.id == owner_id))
            await session.execute(text("DELETE FROM procrastinate_jobs"))
            await session.commit()
        await database.dispose()

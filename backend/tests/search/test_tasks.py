"""The ``index`` step in the real pipeline, the embedding job, the CLI and the gateway
embedder. All data is synthetic."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from app.ai.llm import EnvConfigResolver, LLMGateway, LLMTask
from app.cli import cli
from app.core.config import LLMSettings, SearchSettings
from app.mail.models import Message
from app.mail.storage import AttachmentStorage
from app.processing.models import MessageProcessing, StepStatus
from app.processing.steps import registry
from app.processing.tasks import enqueue_processing
from app.search import service, tasks
from app.search.embedder import GatewayEmbedder
from app.search.models import SearchChunk, SearchEmbedding
from app.worker import TASK_MODULES, app
from tests.ai.fakes import FakeFactory, FakeProvider, RecordingSink
from tests.processing.conftest import Pipeline
from tests.search.conftest import FakeEmbedder


def test_index_step_is_registered() -> None:
    step = registry.get("index")

    assert "app.search.tasks" in TASK_MODULES
    assert step is not None
    assert (step.queue, step.version, step.depends_on) == ("llm", 1, ())
    assert "search.fill_embeddings" in app.tasks


async def test_gateway_embedder_batches_and_overrides_the_model() -> None:
    provider = FakeProvider()
    sink = RecordingSink()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_embedding_model="embed-new")),
        provider_factory=FakeFactory(default=provider),
        metrics=sink,
    )
    embedder = GatewayEmbedder(gateway, SearchSettings(embed_batch_size=2))

    vectors = await embedder.embed(["a", "bb", "ccc", "dddd", "e"])
    old = await embedder.embed(["x"], model="embed-old")

    assert await embedder.current_model() == "embed-new"
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 1.0]
    assert len(old) == 1
    assert [(r.model, r.task) for r in sink.records] == [("embed-new", "embeddings")] * 3 + [
        ("embed-old", "embeddings")
    ]
    assert (await gateway.assignment(LLMTask.EMBEDDINGS)).model == "embed-new"


async def _index(pipeline: Pipeline, embedder: FakeEmbedder, message_id: uuid.UUID) -> None:
    async with pipeline.database.sessionmaker() as session:
        await service.index_message(
            session,
            message_id,
            embedder=embedder,
            storage=AttachmentStorage(Path("/nonexistent")),
            settings=SearchSettings(),
        )
        await session.commit()


async def _count(pipeline: Pipeline, model: type[SearchChunk] | type[SearchEmbedding]) -> int:
    async with pipeline.database.sessionmaker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _set_body(pipeline: Pipeline, message_id: uuid.UUID, body: str) -> None:
    async with pipeline.database.sessionmaker() as session:
        message = await session.get(Message, message_id)
        assert message is not None
        message.body_text = message.body_main = body
        message.subject = "Trip"
        await session.commit()


@pytest.mark.db
async def test_pipeline_runs_the_index_step(indexed: Pipeline, fake_embedder: FakeEmbedder) -> None:
    pipeline = indexed
    step = registry.get("index")
    assert step is not None
    with registry.isolated(step):
        message_id = await pipeline.add_message()
        await _set_body(pipeline, message_id, "Boarding starts at gate 12.")

        async with app.open_async():
            await enqueue_processing(message_id)
        await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        row = await session.scalar(
            select(MessageProcessing).where(MessageProcessing.message_id == message_id)
        )
        chunks = list(await session.scalars(select(SearchChunk)))
    assert row is not None and (row.step, row.status) == ("index", StepStatus.DONE)
    assert [(c.message_id, c.content) for c in chunks] == [
        (message_id, "Boarding starts at gate 12.")
    ]
    assert await _count(pipeline, SearchEmbedding) == 1


@pytest.mark.db
async def test_fill_embeddings_job_switches_the_model(
    indexed: Pipeline, fake_embedder: FakeEmbedder
) -> None:
    pipeline = indexed
    for _ in range(2):
        message_id = await pipeline.add_message()
        await _set_body(pipeline, message_id, "Flight to the airport.")
        await _index(pipeline, fake_embedder, message_id)
    fake_embedder.model = "fake-b"

    async with app.open_async():
        await tasks.fill_embeddings.defer_async()
    await pipeline.drain()

    async with pipeline.database.sessionmaker() as session:
        models = set(await session.scalars(select(SearchEmbedding.model)))
        active = await service.active_model(session)
    assert (models, active) == ({"fake-b"}, "fake-b")


@pytest.mark.db
async def test_fill_embeddings_job_survives_llm_outage(
    indexed: Pipeline, fake_embedder: FakeEmbedder
) -> None:
    pipeline = indexed
    message_id = await pipeline.add_message()
    await _set_body(pipeline, message_id, "Lunch?")
    fake_embedder.fail = True
    await _index(pipeline, fake_embedder, message_id)

    await tasks.fill_embeddings()

    assert await _count(pipeline, SearchEmbedding) == 0
    fake_embedder.fail = False
    await tasks.fill_embeddings()
    assert await _count(pipeline, SearchEmbedding) == 1


def test_cli_lists_search_commands() -> None:
    result = CliRunner().invoke(cli, ["search", "--help"])

    assert result.exit_code == 0
    assert "status" in result.output and "resize" in result.output

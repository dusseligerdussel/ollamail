"""RAG stage with PostgreSQL, the real search index and the fake "perfect" model."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.core.config import LLMSettings, Settings
from app.evals.dataset import Dataset
from app.evals.metrics import RecordingSink
from app.evals.rag import eval_inbox, run_rag
from app.evals.runner import RunOptions, run
from app.mail.models import Message
from app.search.embedder import GatewayEmbedder
from app.search.service import embedding_dimensions
from tests.evals.conftest import DIMENSIONS, Oracle, factory

pytestmark = pytest.mark.db


async def _count_messages(url: str) -> tuple[int, int]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(select(func.count()).select_from(Message))
            async with AsyncSession(bind=connection) as session:
                dimensions = await embedding_dimensions(session)
            return int(count or 0), dimensions
    finally:
        await engine.dispose()


async def test_rag_stage_scores_and_rolls_back(
    migrated_database: str, dataset: Dataset, oracle: Oracle
) -> None:
    sample = dataset.subset(languages={"en"})
    before = await _count_messages(migrated_database)
    settings = Settings(llm=LLMSettings(structured_output_retries=0))
    llm = LLMGateway(
        EnvConfigResolver(settings.llm), provider_factory=factory(oracle), metrics=RecordingSink()
    )
    judge = LLMGateway(EnvConfigResolver(settings.llm), provider_factory=factory(oracle))
    try:
        async with eval_inbox(
            migrated_database, sample, GatewayEmbedder(llm, settings.search), settings
        ) as (inbox, seconds):
            assert len(inbox.mail_ids) == len(sample.mails)
            assert seconds >= 0
            assert await embedding_dimensions(inbox.session) == DIMENSIONS
            report = await run_rag(llm, inbox, sample.questions, settings, judge=judge)
    finally:
        await llm.aclose()
        await judge.aclose()

    result = report.as_dict()
    assert result["questions"] == len(sample.questions)
    assert result["errors"] == 0
    # The fake model always states the expected facts; retrieval is real.
    assert result["answer_correct_rules"] == 1.0
    assert result["no_answer_correct_rules"] == 1.0
    assert result["recall_at_sources"] >= 0.8
    assert result["judge"]["judged"] == len(sample.questions)
    # Rolled back: no mails left, vector column unchanged.
    assert await _count_messages(migrated_database) == before


async def test_runner_includes_rag_with_a_database(
    migrated_database: str, dataset: Dataset, oracle: Oracle
) -> None:
    report = await run(
        dataset.subset(limit=20, languages={"de"}),
        RunOptions(
            models=["perfect:1b"],
            stages=("rag",),
            database_url=migrated_database,
            settings=Settings(llm=LLMSettings(structured_output_retries=0)),
            provider_factory=factory(oracle),
        ),
    )

    assert report.run["stages"] == ["rag"]
    assert report.run["embedding_model"] == "bge-m3"
    rag = report.models[0].stages["rag"]
    assert rag["indexed_mails"] == 20
    assert rag["answer_correct_rules"] in (None, 1.0)
    assert "## Speed" in report.to_markdown()

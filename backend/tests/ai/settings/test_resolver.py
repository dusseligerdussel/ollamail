"""DB-backed resolution: changes apply to the next request without a restart."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.llm.gateway import LLMGateway
from app.ai.llm.types import ChatMessage, LLMTask
from app.ai.settings import store
from app.ai.settings.models import AIProviderRecord, AISettingsRecord
from app.ai.settings.resolver import DbConfigResolver
from app.core.config import DatabaseSettings, LLMSettings
from app.core.db import libpq_url
from tests.ai.fakes import FakeFactory, FakeProvider

pytestmark = pytest.mark.db

MESSAGES = [ChatMessage(role="user", content="Hallo")]
UNREACHABLE_DB = "postgresql+asyncpg://ollamail:ollamail@127.0.0.1:1/ollamail"


@pytest.fixture
async def sessionmaker(scratch_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(scratch_database, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _set_triage_model(
    sessionmaker: async_sessionmaker[AsyncSession], model: str, *, notify: bool = True
) -> None:
    async with sessionmaker() as session:
        record = await store.get_settings_record(session)
        if record is None:
            record = AISettingsRecord(tasks={})
            session.add(record)
        record.tasks = {"triage": {"provider": None, "model": model}}
        if notify:
            await store.notify_changed(session)
        await session.commit()


async def _eventually(check: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(5):
        while not await check():  # noqa: ASYNC110 (polls another process's effect)
            await asyncio.sleep(0.02)


async def test_model_change_applies_to_next_request_via_notify(
    scratch_database: str, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    dsn = libpq_url(DatabaseSettings.model_validate({"url": scratch_database}))
    # A long TTL: only the notification can refresh the snapshot.
    resolver = DbConfigResolver(LLMSettings(), sessionmaker, dsn=dsn, ttl=3600)
    provider = FakeProvider(answers=["a", "b"])
    gateway = LLMGateway(resolver, provider_factory=FakeFactory(default=provider))
    resolver.start()
    try:
        await _set_triage_model(sessionmaker, "model-a")

        async def has(model: str) -> bool:
            return (await resolver.resolve(LLMTask.TRIAGE)).model == model

        await _eventually(lambda: has("model-a"))
        await gateway.complete(LLMTask.TRIAGE, MESSAGES)

        await _set_triage_model(sessionmaker, "model-b")
        await _eventually(lambda: has("model-b"))
        await gateway.complete(LLMTask.TRIAGE, MESSAGES)
    finally:
        await resolver.aclose()
        await gateway.aclose()

    assert [call.model for call in provider.calls] == ["model-a", "model-b"]


async def test_snapshot_is_cached_until_invalidated(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    resolver = DbConfigResolver(LLMSettings(), sessionmaker, ttl=3600)
    await _set_triage_model(sessionmaker, "first", notify=False)
    assert (await resolver.resolve(LLMTask.TRIAGE)).model == "first"

    await _set_triage_model(sessionmaker, "second", notify=False)
    assert (await resolver.resolve(LLMTask.TRIAGE)).model == "first"

    resolver.invalidate()
    assert (await resolver.resolve(LLMTask.TRIAGE)).model == "second"


async def test_snapshot_expires_after_ttl(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    now = 0.0
    resolver = DbConfigResolver(LLMSettings(), sessionmaker, ttl=30, clock=lambda: now)
    await _set_triage_model(sessionmaker, "first", notify=False)
    assert (await resolver.resolve(LLMTask.TRIAGE)).model == "first"

    await _set_triage_model(sessionmaker, "second", notify=False)
    now = 31.0

    assert (await resolver.resolve(LLMTask.TRIAGE)).model == "second"


async def test_cloud_provider_and_api_key_are_loaded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(
            AIProviderRecord(
                name="cloud",
                display_name="Cloud",
                kind="openai_compatible",
                base_url="https://api.test/v1",
                api_key="sk-secret",
                is_cloud=True,
                structured_output="native",
            )
        )
        session.add(
            AISettingsRecord(
                cloud_enabled=True,
                concurrency=2,
                tasks={"digest": {"provider": "cloud", "model": "gpt-test"}},
            )
        )
        await session.commit()
        raw = await session.scalar(text("SELECT api_key FROM ai_providers"))

    resolver = DbConfigResolver(LLMSettings(), sessionmaker)
    digest = await resolver.resolve(LLMTask.DIGEST)

    assert raw is not None and "sk-secret" not in raw
    assert digest.endpoint.api_key == "sk-secret"
    assert digest.endpoint.is_cloud is True
    assert digest.model == "gpt-test"
    assert await resolver.cloud_allowed() is True
    assert await resolver.concurrency() == 2


async def test_unreachable_database_blocks_cloud() -> None:
    engine = create_async_engine(UNREACHABLE_DB, poolclass=NullPool)
    resolver = DbConfigResolver(
        LLMSettings(cloud_enabled=True), async_sessionmaker(engine, expire_on_commit=False)
    )

    assert await resolver.cloud_allowed() is False
    assert (await resolver.resolve(LLMTask.TRIAGE)).endpoint.name == "default"
    await engine.dispose()

import io
import json

import respx
from httpx import ASGITransport, AsyncClient

from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.gateway import LLMGateway, get_llm
from app.ai.llm.metrics import LoggingMetricsSink
from app.ai.llm.types import ChatMessage, LLMTask
from app.core.config import DatabaseSettings, LLMSettings, LoggingSettings, Settings
from app.core.logging import configure_logging
from app.main import create_app
from tests.ai.fakes import FakeFactory, FakeProvider

OLLAMA = "http://ollama.test:11434"
UNREACHABLE_DB = "postgresql+asyncpg://ollamail:ollamail@127.0.0.1:1/ollamail"


def _settings(**llm: object) -> Settings:
    return Settings(
        database=DatabaseSettings.model_validate({"url": UNREACHABLE_DB}),
        llm=LLMSettings.model_validate({"base_url": OLLAMA, **llm}),
    )


async def test_llm_readiness_is_opt_in() -> None:
    app = create_app(_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
    await app.state.database.dispose()

    assert "llm" not in response.json()["checks"]


@respx.mock
async def test_readyz_reports_llm_check() -> None:
    respx.get(f"{OLLAMA}/api/tags").respond(json={"models": [{"name": "qwen2.5:3b"}]})
    app = create_app(_settings(readiness_check=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
    await app.state.database.dispose()

    # bge-m3 (embeddings) is missing.
    assert response.json()["checks"]["llm"] == "failed"

    respx.get(f"{OLLAMA}/api/tags").respond(
        json={"models": [{"name": "qwen2.5:3b"}, {"name": "bge-m3:latest"}]}
    )
    app = create_app(_settings(readiness_check=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
    await app.state.database.dispose()

    assert response.json()["checks"]["llm"] == "ok"


async def test_gateway_is_available_as_dependency() -> None:
    app = create_app(_settings())

    assert isinstance(app.state.llm, LLMGateway)

    class _Request:
        def __init__(self) -> None:
            self.app = app

    assert get_llm(_Request()) is app.state.llm  # type: ignore[arg-type]
    await app.state.database.dispose()


@respx.mock
async def test_pull_missing_models_pulls_only_missing() -> None:
    respx.get(f"{OLLAMA}/api/tags").respond(json={"models": [{"name": "qwen2.5:3b"}]})
    pull = respx.post(f"{OLLAMA}/api/pull").respond(json={"status": "success"})
    gateway = LLMGateway(EnvConfigResolver(LLMSettings(base_url=OLLAMA)))

    await gateway.pull_missing_models()
    await gateway.aclose()

    pulled = [json.loads(call.request.content)["model"] for call in pull.calls]
    assert pulled == ["bge-m3:latest"]


@respx.mock
async def test_pull_failure_is_logged_not_raised() -> None:
    respx.get(f"{OLLAMA}/api/tags").respond(500)
    gateway = LLMGateway(EnvConfigResolver(LLMSettings(base_url=OLLAMA)))

    await gateway.pull_missing_models()
    await gateway.aclose()


async def test_metrics_log_contains_no_content() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG", format="json"), stream=stream)
    secret = "Hello Bob, the merger is confidential"
    provider = FakeProvider(answers=[f"Summary: {secret}"])
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings()),
        provider_factory=FakeFactory(default=provider),
        metrics=LoggingMetricsSink(),
    )

    await gateway.complete(
        LLMTask.DIGEST, [ChatMessage(role="user", content=secret)], prompt_version="digest@1"
    )

    output = stream.getvalue()
    assert secret not in output
    (record,) = [json.loads(line) for line in output.splitlines() if "llm_call" in line]
    assert record["model"] == "qwen2.5:3b"
    assert record["prompt_version"] == "digest@1"
    assert record["prompt_tokens"] == 10
    assert record["completion_tokens"] == 5
    assert "duration_ms" in record

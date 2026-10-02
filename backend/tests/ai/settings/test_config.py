"""Effective configuration: admin overrides on top of the environment."""

import asyncio

import pytest

from app.ai.llm.config import (
    AIOverrides,
    EndpointConfig,
    EnvConfigResolver,
    ResolvedConfig,
    TaskOverride,
)
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.limiter import DynamicLimiter
from app.ai.llm.profiles import PROFILES
from app.ai.llm.types import LLMTask
from app.core.config import LLMSettings
from tests.ai.fakes import FakeFactory, FakeProvider

CLOUD = EndpointConfig(
    name="cloud", provider="openai_compatible", base_url="https://api.test/v1", is_cloud=True
)


def test_without_overrides_the_environment_applies() -> None:
    config = ResolvedConfig(LLMSettings(task_digest_model="big"))

    assert config.assignment(LLMTask.TRIAGE).model == PROFILES["cpu"].chat_model
    assert config.assignment(LLMTask.DIGEST).model == "big"
    assert config.cloud_enabled is False
    assert config.concurrency == 1


def test_overrides_beat_the_environment() -> None:
    settings = LLMSettings(task_triage_model="env-model", cloud_enabled=False, concurrency=2)
    overrides = AIOverrides(
        endpoints={"cloud": CLOUD},
        cloud_enabled=True,
        profile="gpu-server",
        concurrency=3,
        tasks={LLMTask.TRIAGE: TaskOverride(endpoint="cloud", model="gpt-test")},
    )

    config = ResolvedConfig(settings, overrides)
    triage = config.assignment(LLMTask.TRIAGE)
    todos = config.assignment(LLMTask.TODOS)

    assert (triage.endpoint.name, triage.model) == ("cloud", "gpt-test")
    assert (todos.endpoint.name, todos.model) == ("default", PROFILES["gpu-server"].chat_model)
    assert todos.context_tokens == PROFILES["gpu-server"].context_tokens
    assert config.cloud_enabled is True
    assert config.concurrency == 3


def test_environment_endpoint_wins_over_stored_one_of_the_same_name() -> None:
    stored = EndpointConfig(name="default", provider="ollama", base_url="http://stored:11434")

    config = ResolvedConfig(LLMSettings(), AIOverrides(endpoints={"default": stored}))

    assert config.endpoints["default"].base_url == "http://ollama:11434"


def test_assignment_to_unknown_endpoint_falls_back() -> None:
    overrides = AIOverrides(tasks={LLMTask.TODOS: TaskOverride(endpoint="gone", model="m")})

    todos = ResolvedConfig(LLMSettings(), overrides).assignment(LLMTask.TODOS)

    assert todos.endpoint.name == "default"
    assert todos.model == "m"


async def test_limiter_caps_parallel_holders_and_follows_changes() -> None:
    limit = 1
    running = 0
    peak = 0

    async def current() -> int:
        return limit

    limiter = DynamicLimiter(current)

    async def work() -> None:
        nonlocal running, peak
        async with limiter.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(work() for _ in range(4)))
    assert peak == 1

    limit = 3
    await asyncio.gather(*(work() for _ in range(6)))
    assert peak == 3
    assert limiter.active == 0


async def test_gateway_respects_concurrency() -> None:
    class SlowProvider(FakeProvider):
        active = 0
        peak = 0

        async def embed(self, texts: object, *, model: str) -> list[list[float]]:
            SlowProvider.active += 1
            SlowProvider.peak = max(SlowProvider.peak, SlowProvider.active)
            await asyncio.sleep(0.01)
            SlowProvider.active -= 1
            return [[0.0]]

    async def two() -> int:
        return 2

    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings()),
        provider_factory=FakeFactory(default=SlowProvider()),
        concurrency=two,
    )

    await asyncio.gather(*(gateway.embed(["x"]) for _ in range(5)))

    assert SlowProvider.peak == 2


@pytest.mark.parametrize("task", list(LLMTask))
def test_every_task_resolves(task: LLMTask) -> None:
    assert ResolvedConfig(LLMSettings()).assignment(task).endpoint.name == "default"

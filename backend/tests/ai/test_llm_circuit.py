"""Circuit breaker of the LLM gateway (#138): a dead endpoint is not called again and again."""

import pytest

from app.ai.llm.circuit import CircuitBreaker
from app.ai.llm.config import EnvConfigResolver
from app.ai.llm.errors import (
    LLMCircuitOpenError,
    LLMOutputError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelNotAvailableError,
)
from app.ai.llm.gateway import LLMGateway
from app.ai.llm.types import ChatMessage, LLMTask
from app.core.config import LLMSettings
from tests.ai.fakes import FakeFactory, FakeProvider, RecordingSink

MESSAGES = [ChatMessage(role="user", content="Synthetic test mail")]
DOWN = LLMUnavailableError("LLM endpoint unreachable")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _breaker(clock: Clock, threshold: int = 3, cooldown: float = 30) -> CircuitBreaker:
    return CircuitBreaker(threshold=threshold, cooldown=cooldown, clock=clock)


def test_opens_after_consecutive_failures(clock: Clock) -> None:
    breaker = _breaker(clock)
    for _ in range(2):
        breaker.before_call("ollama")
        breaker.record("ollama", DOWN)
    assert not breaker.is_open("ollama")

    breaker.before_call("ollama")
    breaker.record("ollama", DOWN)

    assert breaker.is_open("ollama")
    with pytest.raises(LLMCircuitOpenError) as raised:
        breaker.before_call("ollama")
    assert raised.value.retry_after == pytest.approx(30)
    # Other endpoints are not affected.
    breaker.before_call("gpu")


def test_success_resets_the_failure_count(clock: Clock) -> None:
    breaker = _breaker(clock)
    for error in (DOWN, DOWN, None, DOWN, DOWN):
        breaker.before_call("ollama")
        breaker.record("ollama", error)

    assert not breaker.is_open("ollama")


@pytest.mark.parametrize(
    "error",
    [LLMTimeoutError("slow"), ModelNotAvailableError("m"), LLMOutputError("Triage", 2)],
)
def test_errors_of_a_reachable_endpoint_do_not_count(clock: Clock, error: Exception) -> None:
    breaker = _breaker(clock, threshold=1)
    breaker.before_call("ollama")
    breaker.record("ollama", error)

    assert not breaker.is_open("ollama")


def test_one_probe_after_the_cooldown_and_doubling_while_down(clock: Clock) -> None:
    breaker = _breaker(clock, threshold=1)
    breaker.before_call("ollama")
    breaker.record("ollama", DOWN)

    clock.now += 30
    breaker.before_call("ollama")  # the probe
    with pytest.raises(LLMCircuitOpenError):
        breaker.before_call("ollama")  # only one at a time
    breaker.record("ollama", DOWN)

    # Down again: twice the cooldown.
    clock.now += 59
    with pytest.raises(LLMCircuitOpenError) as raised:
        breaker.before_call("ollama")
    assert raised.value.retry_after == pytest.approx(1)


def test_cooldown_is_capped(clock: Clock) -> None:
    breaker = _breaker(clock, threshold=1, cooldown=10)
    for _ in range(10):
        clock.now += 10_000
        breaker.before_call("ollama")
        breaker.record("ollama", DOWN)

    with pytest.raises(LLMCircuitOpenError) as raised:
        breaker.before_call("ollama")
    assert raised.value.retry_after == pytest.approx(160)


def test_successful_probe_closes(clock: Clock) -> None:
    breaker = _breaker(clock, threshold=1)
    breaker.before_call("ollama")
    breaker.record("ollama", DOWN)
    clock.now += 30
    breaker.before_call("ollama")
    breaker.record("ollama", None)

    assert not breaker.is_open("ollama")
    breaker.before_call("ollama")


def test_cancelled_probe_lets_the_next_call_probe(clock: Clock) -> None:
    breaker = _breaker(clock, threshold=1)
    breaker.before_call("ollama")
    breaker.record("ollama", DOWN)
    clock.now += 30
    breaker.before_call("ollama")
    breaker.release("ollama")

    breaker.before_call("ollama")


async def test_gateway_stops_calling_a_dead_endpoint(clock: Clock) -> None:
    provider = FakeProvider(answers=[DOWN, DOWN, "ok"])
    sink = RecordingSink()
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b")),
        provider_factory=FakeFactory(default=provider),
        metrics=sink,
        circuit=_breaker(clock, threshold=2),
    )

    for _ in range(2):
        with pytest.raises(LLMUnavailableError):
            await gateway.complete(LLMTask.TRIAGE, MESSAGES)
    for _ in range(5):
        with pytest.raises(LLMCircuitOpenError):
            await gateway.complete(LLMTask.TRIAGE, MESSAGES)
        with pytest.raises(LLMCircuitOpenError):
            await gateway.embed(["x"])

    # Only the two real calls reached the endpoint (and the metrics).
    assert len(provider.calls) == 2
    assert len(sink.records) == 2

    clock.now += 30
    result = await gateway.complete(LLMTask.TRIAGE, MESSAGES)
    assert result.content == "ok"
    assert len(provider.calls) == 3


async def test_gateway_without_breaker_always_calls() -> None:
    provider = FakeProvider(answers=[DOWN] * 5)
    gateway = LLMGateway(
        EnvConfigResolver(LLMSettings(default_chat_model="chat:1b")),
        provider_factory=FakeFactory(default=provider),
    )
    for _ in range(5):
        with pytest.raises(LLMUnavailableError) as raised:
            await gateway.complete(LLMTask.TRIAGE, MESSAGES)
        assert not isinstance(raised.value, LLMCircuitOpenError)

    assert len(provider.calls) == 5

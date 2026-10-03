"""Circuit breaker per LLM endpoint.

When Ollama is down, every queued mail would otherwise burn its retries against the dead
endpoint (#138). After ``threshold`` consecutive calls failed because the endpoint was
unreachable, the breaker *opens*: calls fail at once with :class:`LLMCircuitOpenError`
for ``cooldown`` seconds, without touching the network. Then one call may probe the
endpoint (*half-open*); if it fails, the breaker opens again for twice as long (at most
``MAX_COOLDOWN_FACTOR`` times the cooldown), if it succeeds, the breaker closes.

Only "unreachable" counts (``LLMUnavailableError`` except timeouts): a slow answer, a
rejected request or a missing model all come from an endpoint that is up. The state
lives in one process (the worker's gateway); each worker process trips on its own.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from app.ai.llm.errors import LLMCircuitOpenError, LLMTimeoutError, LLMUnavailableError
from app.core.logging import get_logger

log = get_logger(__name__)

MAX_COOLDOWN_FACTOR = 16


def counts_as_down(error: BaseException) -> bool:
    """The call failed because the endpoint is unreachable or broken (5xx, 429)."""
    return isinstance(error, LLMUnavailableError) and not isinstance(
        error, LLMTimeoutError | LLMCircuitOpenError
    )


@dataclass
class _State:
    failures: int = 0
    # Times the breaker opened in a row without a successful call in between.
    trips: int = 0
    open_until: float | None = None
    probing: bool = False


class CircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int,
        cooldown: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._states: dict[str, _State] = {}

    def _state(self, endpoint: str) -> _State:
        return self._states.setdefault(endpoint, _State())

    def is_open(self, endpoint: str) -> bool:
        state = self._states.get(endpoint)
        return state is not None and state.open_until is not None

    def before_call(self, endpoint: str) -> None:
        """Raise :class:`LLMCircuitOpenError` unless a call to ``endpoint`` may go out.
        After the cooldown exactly one call (the probe) goes out at a time."""
        state = self._state(endpoint)
        if state.open_until is None:
            return
        remaining = state.open_until - self._clock()
        if remaining > 0:
            raise LLMCircuitOpenError(endpoint, remaining)
        if state.probing:
            raise LLMCircuitOpenError(endpoint, self._cooldown)
        state.probing = True

    def record(self, endpoint: str, error: BaseException | None) -> None:
        """Result of a call that went out (``error`` is ``None`` on success)."""
        state = self._state(endpoint)
        if error is None or not counts_as_down(error):
            if state.open_until is not None:
                log.info("llm_circuit_closed", endpoint=endpoint)
            self._states[endpoint] = _State()
            return
        state.probing = False
        state.failures += 1
        if state.open_until is None and state.failures < self._threshold:
            return
        cooldown = self._cooldown * min(2**state.trips, MAX_COOLDOWN_FACTOR)
        state.trips += 1
        state.open_until = self._clock() + cooldown
        log.warning(
            "llm_circuit_open",
            endpoint=endpoint,
            cooldown_seconds=round(cooldown),
            error_type=type(error).__name__,
        )

    def release(self, endpoint: str) -> None:
        """A call ended without a result (cancelled): let the next call probe."""
        state = self._states.get(endpoint)
        if state is not None:
            state.probing = False

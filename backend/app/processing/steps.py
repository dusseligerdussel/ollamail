"""Processing steps and their registry.

A feature module (triage, todos, embeddings, ...) registers its step at import time::

    from app.processing.steps import StepContext, registry

    @registry.step("triage", version=1, queue="llm")
    async def classify(ctx: StepContext) -> None:
        ...

and adds itself to ``app.worker.TASK_MODULES`` so the worker (and the CLI) import it.

Contract for handlers
    * Idempotent: a step may run more than once for the same message (retry, duplicate
      job, reprocessing). Replace previous results instead of appending to them.
    * Write only through ``ctx.session`` and do not commit: the pipeline commits the
      handler's writes together with the step status, so both succeed or neither does.
    * Work that must only start once the writes are committed (e.g. queueing a job that
      reads them) goes into ``ctx.after_commit``; it runs after the commit, best effort.
    * Fail with ``StepError("<code>")`` (retried) or ``StepError("<code>", permanent=True)``
      (not retried). Any other exception is retried as well; only its class name is
      stored. Exception texts are never stored or logged (docs/PRIVACY.md).
    * Bump ``version`` whenever the result would change (new prompt, new model logic):
      every message is then processed again by this step, newest first.
"""

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from procrastinate import RetryStrategy
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import QueueName
from app.worker import DEFAULT_RETRY

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class StepContext:
    """What a step handler gets: an open session and the IDs of the message."""

    session: AsyncSession
    message_id: UUID
    mailbox_id: UUID
    # Callbacks run after the step's writes were committed (not after a failed step).
    after_commit: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


StepHandler = Callable[[StepContext], Awaitable[None]]


class StepError(Exception):
    """A step failed with a machine-readable error code (stored, shown to the user).

    The code must not contain content: it is validated as a lower-case identifier.
    """

    def __init__(self, code: str, *, permanent: bool = False) -> None:
        if not _NAME.match(code):
            raise ValueError("error code must match ^[a-z][a-z0-9_]{0,63}$")
        super().__init__(code)
        self.code = code
        self.permanent = permanent


@dataclass(frozen=True)
class ProcessingStep:
    """One step of the pipeline, run once per message (per ``version``)."""

    name: str
    # Bumping the version re-runs the step for all messages.
    version: int
    # Queue of the step's jobs, e.g. ``llm`` for model calls.
    queue: QueueName
    handler: StepHandler = field(compare=False)
    # Steps that must be done (at their current version) before this one runs.
    depends_on: tuple[str, ...] = ()
    # Optional predecessors: if registered, this step waits until they are done *or*
    # failed; if not registered (e.g. the feature is not installed), they are ignored.
    # For steps that use another step's result when it exists (todos after triage).
    after: tuple[str, ...] = ()
    retry: RetryStrategy = field(default_factory=lambda: DEFAULT_RETRY, compare=False)
    # Skipped for mails older than ``OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS`` unless the
    # mailbox opted in; for expensive LLM steps whose result only matters for recent
    # mail (triage, todos). Steps that ``depends_on`` a skipped step are skipped too.
    recent_only: bool = False


class StepRegistry:
    """All known steps. The pipeline reads it at run time, so modules may register late."""

    def __init__(self) -> None:
        self._steps: dict[str, ProcessingStep] = {}

    def register(self, step: ProcessingStep) -> ProcessingStep:
        if not _NAME.match(step.name):
            raise ValueError(f"invalid step name {step.name!r}")
        if step.version < 1:
            raise ValueError(f"step {step.name!r}: version must be >= 1")
        if step.name in self._steps:
            raise ValueError(f"step {step.name!r} is already registered")
        self._steps[step.name] = step
        return step

    def step(
        self,
        name: str,
        *,
        version: int,
        queue: QueueName,
        depends_on: tuple[str, ...] = (),
        after: tuple[str, ...] = (),
        retry: RetryStrategy = DEFAULT_RETRY,
        recent_only: bool = False,
    ) -> Callable[[StepHandler], StepHandler]:
        """Decorator registering ``handler`` as step ``name``."""

        def decorator(handler: StepHandler) -> StepHandler:
            self.register(
                ProcessingStep(
                    name=name,
                    version=version,
                    queue=queue,
                    handler=handler,
                    depends_on=tuple(depends_on),
                    after=tuple(after),
                    retry=retry,
                    recent_only=recent_only,
                )
            )
            return handler

        return decorator

    def get(self, name: str) -> ProcessingStep | None:
        return self._steps.get(name)

    def ordered(self) -> list[ProcessingStep]:
        """All steps, dependencies first (otherwise in registration order).

        Registered ``after`` steps count as dependencies here. Raises ``ValueError`` for
        unknown dependencies (``depends_on`` only) and cycles.
        """
        result: list[ProcessingStep] = []
        state: dict[str, str] = {}

        def visit(step: ProcessingStep) -> None:
            if state.get(step.name) == "done":
                return
            if state.get(step.name) == "visiting":
                raise ValueError(f"dependency cycle at step {step.name!r}")
            state[step.name] = "visiting"
            for dependency in step.depends_on:
                if dependency not in self._steps:
                    raise ValueError(f"step {step.name!r} depends on unknown {dependency!r}")
                visit(self._steps[dependency])
            for predecessor in step.after:
                if predecessor in self._steps:
                    visit(self._steps[predecessor])
            state[step.name] = "done"
            result.append(step)

        for step in self._steps.values():
            visit(step)
        return result

    @contextmanager
    def isolated(self, *steps: ProcessingStep) -> Iterator["StepRegistry"]:
        """Temporarily replace all steps (tests)."""
        saved = self._steps
        self._steps = {}
        try:
            for step in steps:
                self.register(step)
            yield self
        finally:
            self._steps = saved


# The process-wide registry.
registry = StepRegistry()

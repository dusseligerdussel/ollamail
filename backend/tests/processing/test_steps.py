"""Unit tests: step registry, error codes, retry decisions, wiring. No database."""

import pytest
from procrastinate import RetryStrategy
from procrastinate.jobs import Job
from typer.testing import CliRunner

from app.ai.llm.errors import LLMUnavailableError
from app.cli import cli
from app.core.config import ProcessingSettings, Settings
from app.processing import tasks
from app.processing.steps import ProcessingStep, StepContext, StepError, StepRegistry, registry
from app.processing.tasks import Priority, StepRetry, enqueue_processing, error_code
from app.worker import TASK_MODULES, app


async def _noop(ctx: StepContext) -> None:
    return None


def _step(name: str, *depends_on: str, version: int = 1) -> ProcessingStep:
    return ProcessingStep(
        name=name, version=version, queue="default", handler=_noop, depends_on=depends_on
    )


def test_ordered_puts_dependencies_first() -> None:
    steps = StepRegistry()
    steps.register(_step("todos", "triage", "normalize"))
    steps.register(_step("index", "normalize"))
    steps.register(_step("triage", "normalize"))
    steps.register(_step("normalize"))

    assert [s.name for s in steps.ordered()] == ["normalize", "triage", "todos", "index"]


def test_unknown_dependency_is_rejected() -> None:
    steps = StepRegistry()
    steps.register(_step("triage", "normalize"))

    with pytest.raises(ValueError, match="unknown 'normalize'"):
        steps.ordered()


def test_dependency_cycle_is_rejected() -> None:
    steps = StepRegistry()
    steps.register(_step("a", "b"))
    steps.register(_step("b", "a"))

    with pytest.raises(ValueError, match="cycle"):
        steps.ordered()


@pytest.mark.parametrize(
    "step",
    [_step("Triage"), _step("triage-v2"), _step("x" * 65), _step("triage", version=0)],
)
def test_invalid_steps_are_rejected(step: ProcessingStep) -> None:
    with pytest.raises(ValueError):
        StepRegistry().register(step)


def test_duplicate_names_are_rejected() -> None:
    steps = StepRegistry()
    steps.register(_step("triage"))

    with pytest.raises(ValueError, match="already registered"):
        steps.register(_step("triage"))


def test_step_decorator_registers_handler() -> None:
    steps = StepRegistry()

    @steps.step("triage", version=3, queue="llm", depends_on=("normalize",))
    async def classify(ctx: StepContext) -> None:
        return None

    step = steps.get("triage")
    assert step is not None
    assert (step.version, step.queue, step.depends_on, step.handler) == (
        3,
        "llm",
        ("normalize",),
        classify,
    )


def test_isolated_restores_registry() -> None:
    before = registry.ordered()
    with registry.isolated(_step("dummy")) as steps:
        assert [s.name for s in steps.ordered()] == ["dummy"]
    assert registry.ordered() == before


def test_step_error_code_must_be_an_identifier() -> None:
    with pytest.raises(ValueError):
        StepError("Mail from alice@example.org is too long")
    assert StepError("too_long", permanent=True).permanent


def test_error_code_never_contains_exception_text() -> None:
    assert error_code(StepError("model_output_invalid")) == "model_output_invalid"
    assert error_code(LLMUnavailableError("Re: Quarterly numbers")) == "llm_unavailable_error"
    assert error_code(RuntimeError("alice@example.org")) == "runtime_error"


def _job(step: str, attempts: int) -> Job:
    return Job(
        id=1,
        queue="default",
        lock=None,
        queueing_lock=None,
        task_name="processing.run_step",
        task_kwargs={"message_id": "m", "step": step},
        attempts=attempts,
    )


def test_step_retry_uses_the_strategy_of_the_step() -> None:
    step = ProcessingStep(
        name="triage",
        version=1,
        queue="llm",
        handler=_noop,
        retry=RetryStrategy(max_attempts=2, exponential_wait=3),
    )
    retry = StepRetry()
    with registry.isolated(step):
        first = retry.get_retry_decision(exception=RuntimeError(), job=_job("triage", 0))
        assert first is not None
        assert first.retry_at is not None
        assert retry.get_retry_decision(exception=RuntimeError(), job=_job("triage", 2)) is None
        permanent = StepError("unsupported", permanent=True)
        assert retry.get_retry_decision(exception=permanent, job=_job("triage", 0)) is None


def test_new_mail_has_the_highest_priority() -> None:
    assert Priority.NEW > Priority.BACKFILL > Priority.REPROCESS


def test_pipeline_tasks_are_registered() -> None:
    assert "app.processing.tasks" in TASK_MODULES
    assert {"processing.plan_message", "processing.run_step"} <= set(app.tasks)
    periodic = {task.periodic_id for task in app.periodic_registry.periodic_tasks.values()}
    assert "processing_requeue_outdated" in periodic


async def test_enqueue_is_a_noop_when_processing_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    settings = Settings(processing=ProcessingSettings(enabled=False))
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)

    # The app is not open: deferring would fail.
    await enqueue_processing(uuid.uuid4())


def test_processing_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_PROCESSING_ENABLED", "false")
    monkeypatch.setenv("OLLAMAIL_PROCESSING_REQUEUE_BATCH_SIZE", "50")

    settings = Settings().processing

    assert (settings.enabled, settings.requeue_batch_size) == (False, 50)


def test_cli_lists_processing_commands() -> None:
    result = CliRunner().invoke(cli, ["processing", "--help"])

    assert result.exit_code == 0
    for command in ("reprocess", "enable", "disable"):
        assert command in result.stdout


def test_cli_rejects_unknown_step() -> None:
    with registry.isolated(_step("triage")):
        result = CliRunner().invoke(cli, ["processing", "reprocess", "--step", "summary"])

    assert result.exit_code == 1
    assert "Unknown step(s): summary" in result.stderr

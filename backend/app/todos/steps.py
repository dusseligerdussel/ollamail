"""Pipeline step ``todos`` (listed in ``app.worker.TASK_MODULES``).

Runs after ``triage`` when that step is registered (``after``: a failed or missing triage
does not block it), so the triage category can exclude newsletters, notifications and
spam (``OLLAMAIL_TODOS_SKIP_CATEGORIES``). Bump ``version`` when the prompt or the
extraction logic changes the result.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.ai.llm import LLMGateway
from app.ai.settings.runtime import worker_gateway
from app.core.config import get_settings
from app.core.logging import get_logger
from app.processing.steps import StepContext, registry
from app.todos.extraction import extract_todos

log = get_logger(__name__)

STEP = "todos"

_llm: LLMGateway | None = None


def get_llm() -> LLMGateway:
    """LLM gateway of the worker process, created on first use."""
    global _llm
    if _llm is None:
        # Shared by all jobs of the process: admin settings, limited parallelism.
        _llm = worker_gateway()
    return _llm


@contextmanager
def use_llm(gateway: LLMGateway) -> Iterator[None]:
    """Run the step against ``gateway`` (tests)."""
    global _llm
    saved, _llm = _llm, gateway
    try:
        yield
    finally:
        _llm = saved


@registry.step(STEP, version=3, queue="llm", after=("triage",), recent_only=True)
async def extract(ctx: StepContext) -> None:
    created = await extract_todos(
        ctx.session, ctx.message_id, llm=get_llm(), settings=get_settings().todos
    )
    # Counts only; titles are mail content (docs/PRIVACY.md).
    log.info("todos_extracted", message_id=str(ctx.message_id), created=len(created))

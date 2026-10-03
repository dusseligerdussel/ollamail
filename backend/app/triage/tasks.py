"""Triage in the processing pipeline (docs/ARCHITECTURE.md §4.1, §4.2).

* Step ``triage`` (queue ``llm``): pre-filter or LLM classification of a new message.
* Step ``triage_write_back`` (queue ``sync``): writes the category to the server if the
  mailbox opted in; a no-op otherwise.
* Periodic job ``triage.write_back`` (every minute): writes back corrections and the
  messages of mailboxes where write-back was just enabled.
* The todo step (``app.todos.steps``) runs after ``triage`` and reads the category via
  ``category_key`` to skip bulk mail.

Bump ``TRIAGE_STEP_VERSION`` together with the prompt version (``app.triage.prompts``):
all messages are then triaged again, newest first; user corrections are kept.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.ai.llm import LLMGateway
from app.ai.settings.runtime import worker_gateway
from app.core.config import get_settings
from app.core.logging import get_logger
from app.mail.providers.base import ProviderError
from app.mail.providers.registry import UnknownProviderError
from app.processing.steps import StepContext, StepError, registry
from app.processing.tasks import get_database
from app.todos.extraction import set_category_lookup
from app.triage.service import category_key, publish_triaged, triage_message
from app.triage.writeback import pending_by_mailbox, write_back_messages
from app.worker import DEFAULT_RETRY, app

log = get_logger(__name__)

TRIAGE_STEP_VERSION = 2
# Pending write-backs handled per run of the periodic job.
WRITE_BACK_BATCH_SIZE = 200

_llm: LLMGateway | None = None


def install_category_lookup() -> None:
    """Let the todo step read the triage category (it skips newsletters, notifications and
    spam, ``OLLAMAIL_TODOS_SKIP_CATEGORIES``). Called when the worker imports this module."""
    set_category_lookup(category_key)


install_category_lookup()


def get_llm() -> LLMGateway:
    """LLM gateway of the worker process, created on first use."""
    global _llm
    if _llm is None:
        # Shared by all jobs of the process: admin settings, limited parallelism.
        _llm = worker_gateway()
    return _llm


@contextmanager
def use_llm(gateway: LLMGateway) -> Iterator[None]:
    """Run the triage against ``gateway`` (tests, evaluation)."""
    global _llm
    saved, _llm = _llm, gateway
    try:
        yield
    finally:
        _llm = saved


@registry.step("triage", version=TRIAGE_STEP_VERSION, queue="llm", recent_only=True)
async def triage_step(ctx: StepContext) -> None:
    result = await triage_message(
        ctx.session, ctx.message_id, llm=get_llm(), settings=get_settings().triage
    )
    if result is not None:
        await publish_triaged(ctx.session, ctx.message_id, ctx.mailbox_id)


@registry.step("triage_write_back", version=1, queue="sync", depends_on=("triage",))
async def write_back_step(ctx: StepContext) -> None:
    try:
        await write_back_messages(
            ctx.session,
            ctx.mailbox_id,
            [ctx.message_id],
            prefix=get_settings().triage.label_prefix,
        )
    except UnknownProviderError:
        raise StepError("mail_provider_unavailable", permanent=True) from None
    except ProviderError as exc:
        raise StepError(exc.code) from None


@app.periodic(cron="* * * * *", periodic_id="triage_write_back")
@app.task(
    name="triage.write_back",
    queue="sync",
    queueing_lock="triage.write_back",
    lock="triage.write_back",
    retry=DEFAULT_RETRY,
)
async def write_back_pending(timestamp: int) -> None:
    """Every minute: write back pending results of mailboxes with write-back enabled.

    A mailbox that fails is skipped until the next run; the others continue.
    """
    prefix = get_settings().triage.label_prefix
    database = get_database()
    async with database.sessionmaker() as session:
        pending = await pending_by_mailbox(session, WRITE_BACK_BATCH_SIZE)
    for mailbox_id, message_ids in pending.items():
        try:
            async with database.sessionmaker() as session:
                await write_back_messages(session, mailbox_id, message_ids, prefix=prefix)
                await session.commit()
        except (ProviderError, UnknownProviderError) as exc:
            log.warning(
                "triage_write_back_failed",
                mailbox_id=str(mailbox_id),
                error_type=type(exc).__name__,
            )

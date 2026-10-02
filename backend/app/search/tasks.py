"""Worker side of the index: the ``index`` processing step and embedding maintenance.

``index`` runs for every message on the ``llm`` queue (its embeddings need the model).
``search.fill_embeddings`` runs every 5 minutes, also on ``llm`` and behind all mail
processing (``Priority.REPROCESS``): it embeds chunks that have no vector of the current
embedding model yet. After a model switch (``OLLAMAIL_LLM_TASK_EMBEDDINGS_MODEL`` or the
hardware profile) this rebuilds the vector index batch by batch while the previous
model's vectors keep answering queries, then switches over and removes the old vectors.
"""

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager

from procrastinate.exceptions import AlreadyEnqueued

from app.ai.llm import LLMError
from app.ai.settings.runtime import worker_gateway
from app.core.config import get_settings
from app.core.logging import get_logger
from app.mail.storage import AttachmentStorage
from app.processing.steps import StepContext, registry
from app.processing.tasks import Priority, get_database
from app.search import service
from app.search.embedder import Embedder, EmbeddingDimensionError, GatewayEmbedder
from app.worker import DEFAULT_RETRY, app

log = get_logger(__name__)

FILL_LOCK = "search.fill_embeddings"

_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """Embedder of the worker process, created on first use."""
    global _embedder
    if _embedder is None:
        settings = get_settings()
        _embedder = GatewayEmbedder(worker_gateway(), settings.search)
    return _embedder


@contextmanager
def use_embedder(embedder: Embedder) -> Iterator[None]:
    """Use ``embedder`` in jobs (tests)."""
    global _embedder
    saved, _embedder = _embedder, embedder
    try:
        yield
    finally:
        _embedder = saved


@registry.step("index", version=1, queue="llm")
async def index(ctx: StepContext) -> None:
    """Chunks, full-text vector and embeddings of one message (body and attachments)."""
    settings = get_settings()
    result = await service.index_message(
        ctx.session,
        ctx.message_id,
        embedder=get_embedder(),
        storage=AttachmentStorage(settings.storage.data_dir),
        settings=settings.search,
    )
    log.info(
        "search_message_indexed",
        message_id=str(ctx.message_id),
        chunks=result.chunks,
        embedded=result.embedded,
        extraction=dict(result.extraction),
    )


async def _defer_fill() -> None:
    with contextlib.suppress(AlreadyEnqueued):
        await fill_embeddings.configure(
            priority=int(Priority.REPROCESS), queueing_lock=FILL_LOCK
        ).defer_async()


@app.periodic(
    cron="*/5 * * * *", periodic_id="search_fill_embeddings", priority=int(Priority.REPROCESS)
)
@app.task(
    name="search.fill_embeddings",
    queue="llm",
    lock=FILL_LOCK,
    queueing_lock=FILL_LOCK,
    retry=DEFAULT_RETRY,
)
async def fill_embeddings(timestamp: int | None = None) -> None:
    """Embed one batch of chunks without a vector of the current model; queue the next
    batch right away while more are missing."""
    settings = get_settings()
    async with get_database().sessionmaker() as session:
        try:
            result = await service.fill_embeddings(session, get_embedder(), settings.search)
        except (LLMError, EmbeddingDimensionError) as exc:
            # Not retried: the next periodic run tries again.
            log.warning("search_fill_embeddings_failed", error_type=type(exc).__name__)
            return
        await session.commit()
    if result.embedded or result.switched:
        log.info(
            "search_embeddings_filled",
            count=result.embedded,
            remaining=result.remaining,
            switched=result.switched,
        )
    if result.remaining:
        await _defer_fill()

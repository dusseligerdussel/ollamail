"""Worker side of the index: the ``index`` processing step and embedding maintenance.

``index`` runs for every message on the ``llm`` queue (its embeddings need the model).
``search.fill_embeddings`` runs every 5 minutes, also on ``llm`` and behind all mail
processing (``Priority.REPROCESS``): it embeds chunks that have no vector of the current
embedding model yet. After a model switch (``OLLAMAIL_LLM_TASK_EMBEDDINGS_MODEL`` or the
hardware profile) this rebuilds the vector index batch by batch while the previous
model's vectors keep answering queries, then switches over and removes the old vectors.
``search.ocr_attachment`` runs on the ``ocr`` queue (own job slots, lowest priority): it
recognises scanned attachments that ``index`` reported and stores their chunks without
vectors, which ``fill_embeddings`` then adds. It never calls the LLM.
"""

import contextlib
import uuid
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
from app.worker import DEFAULT_RETRY, app, resource_lock

log = get_logger(__name__)

FILL_LOCK = "search.fill_embeddings"
# Below every mail processing job; OCR has its own queue, this orders it within.
OCR_PRIORITY = -20

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
    for attachment_id in result.ocr_pending:
        await _defer_ocr(ctx.message_id, attachment_id)


def _index_lock(message_id: uuid.UUID | str) -> str:
    # Lock of the message's ``index`` step job (``app.processing.tasks._step_lock``): the
    # OCR job starts only after that job has committed, and a re-index waits for OCR.
    return resource_lock("message_step", f"{message_id}:index")


async def _defer_ocr(message_id: uuid.UUID, attachment_id: uuid.UUID) -> None:
    with contextlib.suppress(AlreadyEnqueued):
        await ocr_attachment.configure(
            priority=OCR_PRIORITY,
            lock=_index_lock(message_id),
            queueing_lock=resource_lock("attachment_ocr", attachment_id),
        ).defer_async(message_id=str(message_id), attachment_id=str(attachment_id))


@app.task(name="search.ocr_attachment", queue="ocr", retry=DEFAULT_RETRY)
async def ocr_attachment(message_id: str, attachment_id: str) -> None:
    """Text recognition of one attachment; replaces its chunks. Failures (timeout, broken
    scan, Tesseract missing) are logged as status code only and not retried."""
    settings = get_settings()
    async with get_database().sessionmaker() as session:
        result = await service.ocr_attachment(
            session,
            uuid.UUID(attachment_id),
            storage=AttachmentStorage(settings.storage.data_dir),
            settings=settings.search,
        )
        await session.commit()
    log.info(
        "search_attachment_ocr",
        message_id=message_id,
        attachment_id=attachment_id,
        status=result.status,
        chunks=result.chunks,
    )
    if result.chunks:
        await _defer_fill()


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

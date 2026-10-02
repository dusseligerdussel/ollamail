"""Write and query the hybrid index.

* :func:`index_message`: chunks of a message (body + attachment text) with full-text
  vector and embeddings; replaces the previous chunks of that message.
* :func:`fill_embeddings`: embeds chunks that have no vector of the current model yet
  (model switch, LLM unavailable while indexing) and switches the active model once the
  new index is complete.
* :func:`search`: full-text and vector candidates, fused with Reciprocal Rank Fusion,
  restricted in SQL to the mailboxes the user may read.

Nothing here logs content: only IDs, counts and status codes.
"""

import asyncio
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    delete,
    exists,
    func,
    literal_column,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.llm import LLMError
from app.core.config import SearchSettings
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.access import accessible_mailbox_ids
from app.mail.language import detect_language
from app.mail.models import Attachment, Message, message_folders
from app.mail.storage import AttachmentStorage
from app.search.chunking import Chunk, chunk_text, heading, ts_config_for
from app.search.chunking import clean as clean_text
from app.search.embedder import Embedder, EmbeddingDimensionError, check_dimensions
from app.search.extract import Extraction, detect_kind, extract_text
from app.search.models import (
    TS_CONFIGS,
    ChunkSource,
    SearchChunk,
    SearchEmbedding,
    SearchIndexState,
)
from app.triage.models import TriageResult

log = get_logger(__name__)

STATE_KEY = "embeddings"

_EMBEDDING_FAILURES = (LLMError, EmbeddingDimensionError)


# --- index state ---------------------------------------------------------------------


async def embedding_dimensions(session: AsyncSession) -> int:
    """Length of the vectors the database column accepts (authoritative over settings)."""
    result = await session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute"
            " WHERE attrelid = 'search_embeddings'::regclass AND attname = 'embedding'"
        )
    )
    return int(result.scalar_one())


async def active_model(session: AsyncSession) -> str | None:
    """Model whose vectors answer queries; ``None`` before anything was embedded."""
    result = await session.scalar(
        select(SearchIndexState.active_model).where(SearchIndexState.key == STATE_KEY)
    )
    return result


async def _ensure_active_model(session: AsyncSession, current: str) -> str:
    await session.execute(
        insert(SearchIndexState)
        .values(id=uuid7(), key=STATE_KEY, active_model=current)
        .on_conflict_do_nothing(index_elements=[SearchIndexState.key])
    )
    model = await active_model(session)
    assert model is not None
    return model


async def _store_embeddings(
    session: AsyncSession,
    chunk_ids: Sequence[uuid.UUID],
    vectors: Sequence[Sequence[float]],
    model: str,
) -> None:
    if not chunk_ids:
        return
    await session.execute(
        insert(SearchEmbedding)
        .values(
            [
                {"id": uuid7(), "chunk_id": chunk_id, "model": model, "embedding": vector}
                for chunk_id, vector in zip(chunk_ids, vectors, strict=True)
            ]
        )
        .on_conflict_do_nothing(index_elements=[SearchEmbedding.chunk_id, SearchEmbedding.model])
    )


async def _embed(
    embedder: Embedder, texts: Sequence[str], dimensions: int, model: str | None = None
) -> list[list[float]]:
    vectors = await embedder.embed(texts, model=model)
    if len(vectors) != len(texts):
        raise EmbeddingDimensionError
    check_dimensions(vectors, dimensions)
    return vectors


# --- indexing ------------------------------------------------------------------------


@dataclass(frozen=True)
class _PendingChunk:
    chunk: Chunk
    source: str
    ts_config: str
    attachment_id: uuid.UUID | None = None


@dataclass
class IndexResult:
    chunks: int = 0
    # Vectors of the current model were stored (otherwise ``fill_embeddings`` adds them).
    embedded: bool = False
    # Extraction status per attachment (``ok``, ``too_large``, ``unsupported``, ...).
    extraction: Counter[str] = field(default_factory=Counter)


async def _attachment_text(
    attachment: Attachment, storage: AttachmentStorage, settings: SearchSettings
) -> Extraction:
    kind = detect_kind(attachment.content_type, attachment.filename)
    if kind is None:
        return Extraction("unsupported")
    if attachment.size > settings.attachment_max_bytes:
        return Extraction("too_large")
    path = attachment.storage_path
    if not await asyncio.to_thread(storage.exists, path):
        return Extraction("missing")
    data = await asyncio.to_thread(storage.read, path)
    return await extract_text(data, kind, settings)


async def _message_chunks(
    message: Message, storage: AttachmentStorage, settings: SearchSettings, result: IndexResult
) -> list[_PendingChunk]:
    date = message.sent_at or message.received_at
    size, overlap = settings.chunk_size, settings.chunk_overlap
    context = heading(sender=message.sender, date=date, subject=message.subject)
    body = message.body_main if message.body_main.strip() else message.body_text
    body_config = ts_config_for(message.language)
    # Every message gets at least one chunk, so sender and subject are always searchable.
    chunks = [
        _PendingChunk(chunk, ChunkSource.BODY, body_config)
        for chunk in chunk_text(body, context=context, size=size, overlap=overlap)
    ] or [_PendingChunk(Chunk(context, ""), ChunkSource.BODY, body_config)]

    for attachment in sorted(message.attachments, key=lambda a: a.id):
        if len(chunks) >= settings.max_chunks_per_message:
            break
        extraction = await _attachment_text(attachment, storage, settings)
        result.extraction[extraction.status] += 1
        if extraction.text is None:
            continue
        attachment_context = heading(
            sender=message.sender,
            date=date,
            subject=message.subject,
            attachment=attachment.filename or "",
        )
        config = ts_config_for(detect_language(extraction.text) or message.language)
        chunks.extend(
            _PendingChunk(chunk, ChunkSource.ATTACHMENT, config, attachment.id)
            for chunk in chunk_text(
                extraction.text, context=attachment_context, size=size, overlap=overlap
            )
        )
    return chunks[: settings.max_chunks_per_message]


async def index_message(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    embedder: Embedder,
    storage: AttachmentStorage,
    settings: SearchSettings,
) -> IndexResult:
    """(Re-)build the index of one message. Idempotent: previous chunks are replaced.

    If embedding fails (LLM unavailable, wrong dimension), the chunks are stored without
    vectors: full-text search finds them right away and ``fill_embeddings`` adds the
    vectors later. Does not commit.
    """
    result = IndexResult()
    message = await session.scalar(
        select(Message).where(Message.id == message_id).options(selectinload(Message.attachments))
    )
    if message is None:
        return result
    pending = await _message_chunks(message, storage, settings, result)
    texts = [item.chunk.text for item in pending]

    current = await embedder.current_model()
    active = await _ensure_active_model(session, current)
    dimensions = await embedding_dimensions(session)
    vectors: dict[str, list[list[float]]] = {}
    # While the index is rebuilt for a new model, the old model's vectors still answer
    # queries; new messages get both so they are found before and after the switch.
    for model in dict.fromkeys((current, active)):
        try:
            vectors[model] = await _embed(embedder, texts, dimensions, model=model)
        except _EMBEDDING_FAILURES as exc:
            log.warning(
                "search_embedding_deferred",
                message_id=str(message_id),
                error_type=type(exc).__name__,
                current_model=model == current,
            )

    await session.execute(delete(SearchChunk).where(SearchChunk.message_id == message_id))
    rows = [
        SearchChunk(
            id=uuid7(),
            message_id=message.id,
            mailbox_id=message.mailbox_id,
            attachment_id=item.attachment_id,
            source=item.source,
            ordinal=ordinal,
            heading=item.chunk.heading,
            content=item.chunk.content,
            ts_config=item.ts_config,
        )
        for ordinal, item in enumerate(pending)
    ]
    session.add_all(rows)
    await session.flush()
    for model, model_vectors in vectors.items():
        await _store_embeddings(session, [row.id for row in rows], model_vectors, model)

    result.chunks = len(rows)
    result.embedded = current in vectors
    return result


# --- embedding maintenance -----------------------------------------------------------


@dataclass(frozen=True)
class FillResult:
    embedded: int
    # More chunks lack a vector of the current model.
    remaining: bool
    # The current model became the active one in this run.
    switched: bool


async def fill_embeddings(
    session: AsyncSession, embedder: Embedder, settings: SearchSettings
) -> FillResult:
    """Embed up to ``reembed_batch_size`` chunks lacking a vector of the current model,
    newest first; switch the active model once none is left. Does not commit.

    Raises ``LLMError`` / ``EmbeddingDimensionError`` when embedding fails.
    """
    current = await embedder.current_model()
    active = await _ensure_active_model(session, current)
    has_current = exists().where(
        SearchEmbedding.chunk_id == SearchChunk.id, SearchEmbedding.model == current
    )
    missing = (
        await session.execute(
            select(SearchChunk.id, SearchChunk.heading, SearchChunk.content)
            .where(~has_current)
            .order_by(SearchChunk.created_at.desc(), SearchChunk.id)
            .limit(settings.reembed_batch_size + 1)
        )
    ).all()
    batch = missing[: settings.reembed_batch_size]
    if batch:
        texts = [Chunk(row.heading, row.content).text for row in batch]
        vectors = await _embed(embedder, texts, await embedding_dimensions(session))
        await _store_embeddings(session, [row.id for row in batch], vectors, current)
    remaining = len(missing) > len(batch)

    switched = False
    if active != current and not remaining:
        await session.execute(
            update(SearchIndexState)
            .where(SearchIndexState.key == STATE_KEY)
            .values(active_model=current)
        )
        switched = True
    if not remaining and (switched or active == current):
        # Vectors of earlier models (finished or abandoned switches).
        await session.execute(delete(SearchEmbedding).where(SearchEmbedding.model != current))
    return FillResult(embedded=len(batch), remaining=remaining, switched=switched)


@dataclass(frozen=True)
class IndexStatus:
    chunks: int
    active_model: str | None
    # Embeddings per model.
    embeddings: dict[str, int]
    dimensions: int


async def index_status(session: AsyncSession) -> IndexStatus:
    counts = await session.execute(
        select(SearchEmbedding.model, func.count()).group_by(SearchEmbedding.model)
    )
    return IndexStatus(
        chunks=int(await session.scalar(select(func.count()).select_from(SearchChunk)) or 0),
        active_model=await active_model(session),
        embeddings={model: int(count) for model, count in counts.all()},
        dimensions=await embedding_dimensions(session),
    )


async def resize_embeddings(session: AsyncSession, dimensions: int, model: str) -> None:
    """Change the vector column to ``dimensions``: all vectors are deleted (they cannot
    be converted), ``model`` becomes the active model and ``fill_embeddings`` rebuilds
    the vectors in the background. Full-text search keeps working. Does not commit."""
    if not 1 <= dimensions <= 2000:
        raise ValueError("dimensions must be between 1 and 2000 (HNSW limit)")
    await session.execute(text("DROP INDEX IF EXISTS ix_search_embeddings_embedding_hnsw"))
    await session.execute(delete(SearchEmbedding))
    await session.execute(
        text(f"ALTER TABLE search_embeddings ALTER COLUMN embedding TYPE vector({dimensions:d})")
    )
    await session.execute(
        text(
            "CREATE INDEX ix_search_embeddings_embedding_hnsw ON search_embeddings"
            " USING hnsw (embedding vector_cosine_ops)"
        )
    )
    await session.execute(delete(SearchIndexState))
    await _ensure_active_model(session, model)


# --- search --------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchFilters:
    """Optional restrictions; all of them narrow the user's readable mailboxes."""

    mailbox_ids: Sequence[uuid.UUID] | None = None
    folder_ids: Sequence[uuid.UUID] | None = None
    # Substring of the sender's address or name (case-insensitive).
    sender: str | None = None
    # Date of the message (sent, else received): ``since <= date < until``.
    since: datetime | None = None
    until: datetime | None = None
    # ``ChunkSource.BODY`` or ``ChunkSource.ATTACHMENT``.
    source: str | None = None
    # Triage categories of the message (``TriageResult.category_id``).
    category_ids: Sequence[uuid.UUID] | None = None


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    message_id: uuid.UUID
    mailbox_id: uuid.UUID
    attachment_id: uuid.UUID | None
    source: str
    heading: str
    content: str
    # Reciprocal Rank Fusion score and the ranks (from 1) in each result list.
    score: float
    text_rank: int | None
    vector_rank: int | None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _conditions(user_id: uuid.UUID, filters: SearchFilters) -> list[ColumnElement[bool]]:
    """WHERE clauses on ``SearchChunk`` joined with ``Message``. Access control first."""
    conditions: list[ColumnElement[bool]] = [
        SearchChunk.mailbox_id.in_(accessible_mailbox_ids(user_id))
    ]
    if filters.mailbox_ids is not None:
        conditions.append(SearchChunk.mailbox_id.in_(list(filters.mailbox_ids)))
    if filters.folder_ids is not None:
        conditions.append(
            exists().where(
                message_folders.c.message_id == SearchChunk.message_id,
                message_folders.c.folder_id.in_(list(filters.folder_ids)),
            )
        )
    date = func.coalesce(Message.sent_at, Message.received_at)
    if filters.since is not None:
        conditions.append(date >= filters.since)
    if filters.until is not None:
        conditions.append(date < filters.until)
    if filters.sender:
        pattern = f"%{_escape_like(filters.sender.strip())}%"
        conditions.append(
            Message.sender["address"].astext.ilike(pattern, escape="\\")
            | Message.sender["name"].astext.ilike(pattern, escape="\\")
        )
    if filters.source is not None:
        conditions.append(SearchChunk.source == filters.source)
    if filters.category_ids is not None:
        conditions.append(
            exists().where(
                TriageResult.message_id == SearchChunk.message_id,
                TriageResult.category_id.in_(list(filters.category_ids)),
            )
        )
    return conditions


def _tsquery(query: str) -> ColumnElement[Any]:
    """The query parsed with every configuration, OR-ed: a German chunk matches the
    German stems, an English one the English stems, others the plain words."""
    parsed = [
        func.websearch_to_tsquery(literal_column(f"'{config}'::regconfig"), query)
        for config in TS_CONFIGS
    ]
    combined: ColumnElement[Any] = parsed[0]
    for item in parsed[1:]:
        combined = combined.op("||")(item)
    return combined


def _ranked(inner: Select[Any]) -> Select[Any]:
    """Chunk IDs of ``inner`` best first (``order_key`` ascending); the position is the rank.

    ``inner`` orders and limits on its own so PostgreSQL can use the index for it."""
    subquery = inner.subquery()
    return select(subquery.c.chunk_id).order_by(subquery.c.order_key, subquery.c.chunk_id)


async def _text_candidates(
    session: AsyncSession, query: str, conditions: list[ColumnElement[bool]], limit: int
) -> list[uuid.UUID]:
    tsquery = _tsquery(query)
    score = (-func.ts_rank_cd(SearchChunk.tsv, tsquery)).label("order_key")
    inner = (
        select(SearchChunk.id.label("chunk_id"), score)
        .join(Message, Message.id == SearchChunk.message_id)
        .where(SearchChunk.tsv.op("@@")(tsquery), *conditions)
        .order_by(score, SearchChunk.id)
        .limit(limit)
    )
    return list((await session.scalars(_ranked(inner))).all())


async def _configure_vector_scan(session: AsyncSession, limit: int) -> None:
    # The HNSW index returns ``ef_search`` candidates before the access filter applies;
    # pgvector >= 0.8 keeps scanning until enough rows pass the filter.
    await session.execute(
        text("SELECT set_config('hnsw.ef_search', :value, true)"),
        {"value": str(min(1000, max(40, limit * 2)))},
    )
    version = await session.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    parts = tuple(int(p) for p in str(version or "0").split(".")[:2] if p.isdigit())
    if parts >= (0, 8):
        await session.execute(
            text("SELECT set_config('hnsw.iterative_scan', 'strict_order', true)")
        )


async def _vector_candidates(
    session: AsyncSession,
    vector: Sequence[float],
    model: str,
    conditions: list[ColumnElement[bool]],
    limit: int,
) -> list[uuid.UUID]:
    await _configure_vector_scan(session, limit)
    distance = SearchEmbedding.embedding.cosine_distance(vector).label("order_key")
    inner = (
        select(SearchEmbedding.chunk_id.label("chunk_id"), distance)
        .join(SearchChunk, SearchChunk.id == SearchEmbedding.chunk_id)
        .join(Message, Message.id == SearchChunk.message_id)
        .where(SearchEmbedding.model == model, *conditions)
        .order_by(distance)
        .limit(limit)
    )
    return list((await session.scalars(_ranked(inner))).all())


async def _query_vector(
    session: AsyncSession, embedder: Embedder, query: str
) -> tuple[list[float], str] | None:
    model = await active_model(session) or await embedder.current_model()
    try:
        vectors = await _embed(embedder, [query], await embedding_dimensions(session), model)
    except _EMBEDDING_FAILURES as exc:
        # Full-text results are still returned.
        log.warning("search_query_embedding_failed", error_type=type(exc).__name__)
        return None
    return vectors[0], model


def fuse(rankings: Sequence[Sequence[uuid.UUID]], k: int) -> list[tuple[uuid.UUID, float]]:
    """Reciprocal Rank Fusion: ``score(d) = Σ 1 / (k + rank(d))`` over all lists in which
    ``d`` occurs (ranks from 1). Ties keep the order of first appearance."""
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


async def search(
    session: AsyncSession,
    user_id: uuid.UUID,
    query: str,
    filters: SearchFilters | None = None,
    *,
    embedder: Embedder | None,
    settings: SearchSettings,
    limit: int = 20,
    per_message: bool = False,
) -> list[SearchHit]:
    """Chunks matching ``query`` in the mailboxes ``user_id`` may read, best first.

    Full-text and vector candidates (``settings.candidates`` each) are fused with
    Reciprocal Rank Fusion. Without ``embedder`` or if the query cannot be embedded, only
    full text is used. ``per_message`` keeps the best chunk per message (classic search);
    otherwise several chunks of one message may be returned (context for answers, #25).
    """
    query = clean_text(query)
    if not query or limit < 1:
        return []
    conditions = _conditions(user_id, filters or SearchFilters())
    candidates = settings.candidates
    text_ids = await _text_candidates(session, query, conditions, candidates)
    vector_ids: list[uuid.UUID] = []
    if embedder is not None:
        query_vector = await _query_vector(session, embedder, query)
        if query_vector is not None:
            vector_ids = await _vector_candidates(
                session, query_vector[0], query_vector[1], conditions, candidates
            )
    fused = fuse([text_ids, vector_ids], settings.rrf_k)
    if not fused:
        return []

    rows = {
        row.id: row
        for row in await session.scalars(
            select(SearchChunk).where(SearchChunk.id.in_([chunk_id for chunk_id, _ in fused]))
        )
    }
    text_rank = {chunk_id: rank for rank, chunk_id in enumerate(text_ids, start=1)}
    vector_rank = {chunk_id: rank for rank, chunk_id in enumerate(vector_ids, start=1)}
    hits: list[SearchHit] = []
    seen: set[uuid.UUID] = set()
    for chunk_id, score in fused:
        row = rows.get(chunk_id)
        if row is None or (per_message and row.message_id in seen):
            continue
        seen.add(row.message_id)
        hits.append(
            SearchHit(
                chunk_id=row.id,
                message_id=row.message_id,
                mailbox_id=row.mailbox_id,
                attachment_id=row.attachment_id,
                source=row.source,
                heading=row.heading,
                content=row.content,
                score=score,
                text_rank=text_rank.get(chunk_id),
                vector_rank=vector_rank.get(chunk_id),
            )
        )
        if len(hits) == limit:
            break
    return hits

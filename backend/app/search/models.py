"""Index tables: chunks (text + full-text vector) and their embeddings.

Both hang off the mail tables with ``ON DELETE CASCADE`` (message, mailbox, attachment),
so deleting mail deletes its index (docs/PRIVACY.md, Löschkonzept). The chunk text is
mail content: it is never logged.

Embeddings live in their own table, tagged with the model that produced them. While the
index is rebuilt for a new model, the rows of the old model keep serving queries
(``SearchIndexState.active_model``) until every chunk has a vector of the new one.
"""

import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.db import Base

# Text search configurations a chunk can be indexed with (``chunking.ts_config_for``).
TS_CONFIGS = ("german", "english", "simple")


def _tsv_for(config: str) -> str:
    # Weight A for the chunk text, B for the header context (sender, date, subject).
    return (
        f"setweight(to_tsvector('{config}'::regconfig, heading), 'B')"
        f" || setweight(to_tsvector('{config}'::regconfig, content), 'A')"
    )


# A generated column must be immutable, so the configuration is a literal per branch
# (casting the text column to ``regconfig`` is not).
TSV_EXPRESSION = (
    "CASE ts_config "
    + " ".join(f"WHEN '{config}' THEN {_tsv_for(config)}" for config in TS_CONFIGS[:-1])
    + f" ELSE {_tsv_for(TS_CONFIGS[-1])} END"
)


class ChunkSource:
    BODY = "body"
    ATTACHMENT = "attachment"


class SearchChunk(Base):
    """A piece of a message's text (body or one attachment) with its header context."""

    __tablename__ = "search_chunks"
    __table_args__ = (
        CheckConstraint(
            "ts_config IN (" + ", ".join(f"'{c}'" for c in TS_CONFIGS) + ")", name="ts_config"
        ),
        Index("ix_search_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(None, "message_id"),
        Index(None, "mailbox_id"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE")
    )
    # Denormalised for the access filter (no join needed to restrict by mailbox).
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mail_mailboxes.id", ondelete="CASCADE")
    )
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mail_attachments.id", ondelete="CASCADE"), index=True
    )
    # ``ChunkSource``: body or attachment.
    source: Mapped[str] = mapped_column(String(16))
    # Position within the message, from 0 (body first, then attachments).
    ordinal: Mapped[int] = mapped_column(SmallInteger)
    # Header context: sender, date, subject (and attachment name).
    heading: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    # Text search configuration from the detected language, one of ``TS_CONFIGS``.
    ts_config: Mapped[str] = mapped_column(String(16))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPRESSION, persisted=True))


class SearchEmbedding(Base):
    """Vector of one chunk for one embedding model."""

    __tablename__ = "search_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model"),
        Index(None, "model"),
        Index(
            "ix_search_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("search_chunks.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(255))
    # The dimension comes from ``OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS``; the database
    # column is authoritative at run time (see ``service.embedding_dimensions``).
    embedding: Mapped[Any] = mapped_column(Vector(get_settings().search.embedding_dimensions))


class SearchIndexState(Base):
    """Which embedding model's vectors answer queries (one row, ``key = 'embeddings'``)."""

    __tablename__ = "search_index_state"

    key: Mapped[str] = mapped_column(String(32), unique=True)
    active_model: Mapped[str] = mapped_column(String(255))

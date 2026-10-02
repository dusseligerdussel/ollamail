"""add search index

Revision ID: e41229c08479
Revises: 173b28ed6d7e
Create Date: 2026-10-01 21:07:34.338487+00:00

The vector column gets ``OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS`` dimensions (default 1024).
A later change of the dimension is not a migration but ``python -m app.cli search resize``
(docs/OPERATIONS.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from app.core.config import SearchSettings


def _tsv(config: str) -> str:
    return (
        f"setweight(to_tsvector('{config}'::regconfig, heading), 'B')"
        f" || setweight(to_tsvector('{config}'::regconfig, content), 'A')"
    )


# Full-text vector per chunk; the configuration must be a literal (immutable expression).
TSV_EXPRESSION = (
    f"CASE ts_config WHEN 'german' THEN {_tsv('german')}"
    f" WHEN 'english' THEN {_tsv('english')}"
    f" ELSE {_tsv('simple')} END"
)

revision: str = "e41229c08479"
down_revision: str | Sequence[str] | None = "173b28ed6d7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_index_state",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("active_model", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_index_state")),
        sa.UniqueConstraint("key", name=op.f("uq_search_index_state_key")),
    )
    op.create_table(
        "search_chunks",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("mailbox_id", sa.Uuid(), nullable=False),
        sa.Column("attachment_id", sa.Uuid(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("ts_config", sa.String(length=16), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                TSV_EXPRESSION,
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ts_config IN ('german', 'english', 'simple')", name=op.f("ck_search_chunks_ts_config")
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["mail_attachments.id"],
            name=op.f("fk_search_chunks_attachment_id_mail_attachments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mailbox_id"],
            ["mail_mailboxes.id"],
            name=op.f("fk_search_chunks_mailbox_id_mail_mailboxes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            name=op.f("fk_search_chunks_message_id_mail_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_chunks")),
    )
    op.create_index(
        op.f("ix_search_chunks_attachment_id"), "search_chunks", ["attachment_id"], unique=False
    )
    op.create_index(
        op.f("ix_search_chunks_mailbox_id"), "search_chunks", ["mailbox_id"], unique=False
    )
    op.create_index(
        op.f("ix_search_chunks_message_id"), "search_chunks", ["message_id"], unique=False
    )
    op.create_index(
        "ix_search_chunks_tsv", "search_chunks", ["tsv"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "search_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("embedding", Vector(SearchSettings().embedding_dimensions), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["search_chunks.id"],
            name=op.f("fk_search_embeddings_chunk_id_search_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_embeddings")),
        sa.UniqueConstraint("chunk_id", "model", name=op.f("uq_search_embeddings_chunk_id")),
    )
    op.create_index(
        "ix_search_embeddings_embedding_hnsw",
        "search_embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        op.f("ix_search_embeddings_model"), "search_embeddings", ["model"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_search_embeddings_model"), table_name="search_embeddings")
    op.drop_index(
        "ix_search_embeddings_embedding_hnsw",
        table_name="search_embeddings",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("search_embeddings")
    op.drop_index("ix_search_chunks_tsv", table_name="search_chunks", postgresql_using="gin")
    op.drop_index(op.f("ix_search_chunks_message_id"), table_name="search_chunks")
    op.drop_index(op.f("ix_search_chunks_mailbox_id"), table_name="search_chunks")
    op.drop_index(op.f("ix_search_chunks_attachment_id"), table_name="search_chunks")
    op.drop_table("search_chunks")
    op.drop_table("search_index_state")

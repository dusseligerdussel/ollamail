"""search embedding backlog

Fewer full scans in ``search.fill_embeddings`` (#224): ``search_embedding_backlog`` lists
the chunks stored without a vector of the current model (``search_index_state.backlog_model``),
so the job no longer compares every chunk with its vectors to find them. The index
``ix_search_chunks_created_at_id`` only served that comparison and is dropped. The backlog
starts out empty with ``backlog_model`` unset, so the job rebuilds it with one scan after
the upgrade. Only chunk IDs are stored, no content.

Revision ID: a6e4b681ab6a
Revises: 368ae36904f1
Create Date: 2026-10-04 19:57:14.152090+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6e4b681ab6a"
down_revision: str | Sequence[str] | None = "368ae36904f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_embedding_backlog",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["search_chunks.id"],
            name=op.f("fk_search_embedding_backlog_chunk_id_search_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_search_embedding_backlog")),
    )
    op.drop_index(op.f("ix_search_chunks_created_at_id"), table_name="search_chunks")
    op.add_column(
        "search_index_state", sa.Column("backlog_model", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("search_index_state", "backlog_model")
    op.create_index(
        op.f("ix_search_chunks_created_at_id"),
        "search_chunks",
        [sa.literal_column("created_at DESC"), "id"],
        unique=False,
    )
    op.drop_table("search_embedding_backlog")

"""processing scan state and fill marker

Fewer full scans (#187): ``processing_scan_state`` remembers the step versions at which
``processing.requeue_outdated`` last found all messages up to date, so it only checks
newly stored messages; ``search_index_state.fill_requested``/``fill_checked`` let
``search.fill_embeddings`` skip its scan while no chunk lacks a vector, and
``ix_search_chunks_created_at_id`` serves the scan when one is needed. The partial index
``ix_message_processing_open`` serves the counts of pending, running and failed steps
(admin overview, metrics). The state rows start out "unchecked", so each job scans once
after the upgrade. No content is stored.

Revision ID: b5c86da9e237
Revises: 4adf661e4588
Create Date: 2026-10-04 12:57:31.199463+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5c86da9e237"
down_revision: str | Sequence[str] | None = "4adf661e4588"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "processing_scan_state",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("step_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("planned_before", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_scan_state")),
        sa.UniqueConstraint("key", name=op.f("uq_processing_scan_state_key")),
    )
    op.create_index(
        "ix_message_processing_open",
        "message_processing",
        ["message_id"],
        unique=False,
        postgresql_where=sa.text("status NOT IN ('done', 'skipped')"),
    )
    op.create_index(
        "ix_search_chunks_created_at_id",
        "search_chunks",
        [sa.literal_column("created_at DESC"), "id"],
        unique=False,
    )
    op.add_column(
        "search_index_state",
        sa.Column("fill_requested", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "search_index_state",
        sa.Column("fill_checked", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("search_index_state", "fill_checked")
    op.drop_column("search_index_state", "fill_requested")
    op.drop_index("ix_search_chunks_created_at_id", table_name="search_chunks")
    op.drop_index(
        "ix_message_processing_open",
        table_name="message_processing",
        postgresql_where=sa.text("status NOT IN ('done', 'skipped')"),
    )
    op.drop_table("processing_scan_state")

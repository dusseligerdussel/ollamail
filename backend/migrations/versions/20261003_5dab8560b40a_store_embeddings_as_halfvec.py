"""store embeddings as halfvec

Embeddings move from ``vector`` (32 bits per dimension) to ``halfvec`` (16 bits), which
halves the table and the HNSW index (#164). Existing vectors are converted in place, so
the index does not need to be re-embedded; the HNSW index is rebuilt with
``halfvec_cosine_ops``. The dimension is read from the column (it may have been changed
with ``python -m app.cli search resize``), not from the settings.

``ALTER COLUMN TYPE`` rewrites the table and the index build reads it again, both under
an exclusive lock: search is unavailable while it runs (docs/OPERATIONS.md, 6.7).
Downgrade converts back to ``vector`` the same way (values keep their 16-bit precision).

Revision ID: 5dab8560b40a
Revises: 5c672b257a5b
Create Date: 2026-10-03 15:44:52.480373+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5dab8560b40a"
down_revision: str | Sequence[str] | None = "5c672b257a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_search_embeddings_embedding_hnsw"
# HNSW indexes ``vector`` columns up to 2000 dimensions.
VECTOR_HNSW_MAX_DIMENSIONS = 2000


def _dimensions() -> int:
    typmod = op.get_bind().scalar(
        sa.text(
            "SELECT atttypmod FROM pg_attribute"
            " WHERE attrelid = 'search_embeddings'::regclass AND attname = 'embedding'"
        )
    )
    return int(typmod)


def _pgvector_version() -> tuple[int, ...]:
    version = op.get_bind().scalar(
        sa.text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    return tuple(int(part) for part in str(version or "0").split(".")[:2] if part.isdigit())


def _convert(column_type: str, opclass: str) -> None:
    dimensions = _dimensions()
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(
        f"ALTER TABLE search_embeddings ALTER COLUMN embedding TYPE {column_type}({dimensions:d})"
        f" USING embedding::{column_type}({dimensions:d})"
    )
    op.execute(f"CREATE INDEX {INDEX} ON search_embeddings USING hnsw (embedding {opclass})")


def upgrade() -> None:
    if _pgvector_version() < (0, 7):
        raise RuntimeError(
            "halfvec needs pgvector 0.7 or newer: update the PostgreSQL image and run"
            " 'ALTER EXTENSION vector UPDATE' (docs/OPERATIONS.md, 6.7)"
        )
    _convert("halfvec", "halfvec_cosine_ops")


def downgrade() -> None:
    if _dimensions() > VECTOR_HNSW_MAX_DIMENSIONS:
        raise RuntimeError(
            f"cannot index vector columns with more than {VECTOR_HNSW_MAX_DIMENSIONS}"
            " dimensions: resize the embeddings first"
        )
    _convert("vector", "vector_cosine_ops")

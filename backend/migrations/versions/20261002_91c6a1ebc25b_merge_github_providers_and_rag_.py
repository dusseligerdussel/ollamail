"""merge github providers and rag conversations

Revision ID: 91c6a1ebc25b
Revises: 8f4d3018c23f, aebf78960f69
Create Date: 2026-10-02 06:01:49.633114+00:00
"""

from collections.abc import Sequence

revision: str = "91c6a1ebc25b"
down_revision: str | Sequence[str] | None = ("8f4d3018c23f", "aebf78960f69")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

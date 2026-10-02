"""merge search index and audit heads

Revision ID: dc06b7bd0f20
Revises: 329b919d6ecb, e41229c08479
Create Date: 2026-10-02 04:30:06.840629+00:00
"""

from collections.abc import Sequence

revision: str = "dc06b7bd0f20"
down_revision: str | Sequence[str] | None = ("329b919d6ecb", "e41229c08479")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

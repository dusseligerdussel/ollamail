"""merge privacy and idp admin heads

Revision ID: 5924aab840f1
Revises: 91038e268d7e, d8dd6f4b5840
Create Date: 2026-10-02 06:52:06.249502+00:00
"""

from collections.abc import Sequence

revision: str = "5924aab840f1"
down_revision: str | Sequence[str] | None = ("91038e268d7e", "d8dd6f4b5840")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

"""bind push subscriptions to sessions

Web Push devices belong to the sign-in session they were registered in (#185): signing out,
revoking the session or its expiry delete them (``ON DELETE CASCADE``). Existing devices
have no session and are deleted; the app registers a subscribed browser again on its next
visit, bound to the session it is opened in.

Revision ID: 93abef19553f
Revises: 4adf661e4588
Create Date: 2026-10-04 12:38:51.124801+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "93abef19553f"
down_revision: str | Sequence[str] | None = "4adf661e4588"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM push_subscriptions")
    op.add_column("push_subscriptions", sa.Column("session_id", sa.Uuid(), nullable=False))
    op.create_index(
        op.f("ix_push_subscriptions_session_id"), "push_subscriptions", ["session_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_push_subscriptions_session_id_auth_sessions"),
        "push_subscriptions",
        "auth_sessions",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_push_subscriptions_session_id_auth_sessions"),
        "push_subscriptions",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_push_subscriptions_session_id"), table_name="push_subscriptions")
    op.drop_column("push_subscriptions", "session_id")

"""procrastinate job queue schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01 00:00:00+00:00
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op
from sqlalchemy.util import await_only

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Vendored instead of read from the installed package, so this revision always creates
# the same schema, whichever Procrastinate version is installed.
SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "procrastinate-3.10.0.sql"


def _execute_script(sql: str) -> None:
    # The script holds many statements and PL/pgSQL bodies. asyncpg only runs such scripts
    # through the simple query protocol, i.e. ``Connection.execute`` without arguments.
    # Runs inside the transaction Alembic opened on this connection.
    driver_connection = op.get_bind().connection.driver_connection
    assert driver_connection is not None
    await_only(driver_connection.execute(sql))


def upgrade() -> None:
    _execute_script(SCHEMA_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    _execute_script(
        """
        DROP TABLE IF EXISTS procrastinate_events, procrastinate_periodic_defers,
            procrastinate_jobs, procrastinate_workers CASCADE;
        DO $$
        DECLARE fn regprocedure;
        BEGIN
            FOR fn IN
                SELECT p.oid::regprocedure FROM pg_proc p
                WHERE p.proname LIKE 'procrastinate\\_%'
                  AND p.pronamespace = current_schema()::regnamespace
            LOOP
                EXECUTE 'DROP FUNCTION ' || fn;
            END LOOP;
        END $$;
        DROP TYPE IF EXISTS procrastinate_job_to_defer_v1, procrastinate_job_event_type,
            procrastinate_job_status;
        """
    )

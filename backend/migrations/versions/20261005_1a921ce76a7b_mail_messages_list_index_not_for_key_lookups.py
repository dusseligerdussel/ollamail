"""mail messages list index not for key lookups

PostgreSQL checks the foreign keys to ``mail_messages`` with
``SELECT 1 FROM ONLY mail_messages x WHERE id = $1 FOR KEY SHARE OF x``, once per inserted
row (#246). A ``VACUUM`` while a large transaction is open writes ``reltuples = 0`` for a
table with many pages; the planner then expects one row from every scan, and every index
with ``id`` as a key column costs the same. On a tie it keeps the most recently created
index, ``ix_mail_messages_mailbox_id_sort_date_id`` (``id`` as its last key column), and
read the whole index for every checked row: 75 s for the links and 67 s for the triage
results of 100k messages instead of 3-5 s.

The list index gets the predicate ``WHERE mailbox_id IS NOT NULL``. It is always true
(``mailbox_id`` is ``NOT NULL``), so the index still holds every message. The planner uses
a partial index only for queries that imply its predicate: every list query does
(``mailbox_id = ...`` or ``IN (...)`` is strict, so it implies ``IS NOT NULL``), a lookup by
``id`` alone does not. So only ``pk_mail_messages`` can serve the foreign key checks (and any
other lookup by ``id``), whatever the statistics. Keys, order and the list plans are
unchanged. Alembic does not compare index predicates, so this revision was written by hand
(``--autogenerate`` found no changes).

Rejected:

- ``(mailbox_id, sort_date DESC) INCLUDE (id)``: fixes the checks as well, but the lists
  then sort ties of ``sort_date`` by ``id`` (incremental sort), and the parallel plan of
  the untriaged triage list read 5200-5800 instead of 3200-3600 rows per page (fails
  ``tests/perf``).
- Recreating the indexes so that the primary key wins the tie: depends on index creation
  order, lost with the next index or ``REINDEX``.
- ``ANALYZE`` after the first import of a mailbox: the checks run inside the open
  transaction, before any job could run, and autoanalyze already follows a commit.

The index is rebuilt with ``CREATE INDEX CONCURRENTLY`` under a temporary name, then the
old one is dropped concurrently and the new one renamed, so the lists keep an index and
writes are not blocked. The build reads the table twice and waits for open transactions
(about 0.5 s per 100k messages here; plan minutes for tens of millions).

Revision ID: 1a921ce76a7b
Revises: 5ef6feddecda
Create Date: 2026-10-05 21:46:19.976510+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1a921ce76a7b"
down_revision: str | Sequence[str] | None = "5ef6feddecda"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_mail_messages_mailbox_id_sort_date_id"
# Name while both indexes exist; a failed concurrent build leaves an invalid index under
# this name, which the next attempt drops first.
NEW_INDEX = f"{INDEX}_new"
COLUMNS = "(mailbox_id, sort_date DESC, id DESC)"


def _rebuild(predicate: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {NEW_INDEX}")
        op.execute(f"CREATE INDEX CONCURRENTLY {NEW_INDEX} ON mail_messages {COLUMNS}{predicate}")
        op.execute(f"DROP INDEX CONCURRENTLY {INDEX}")
        op.execute(f"ALTER INDEX {NEW_INDEX} RENAME TO {INDEX}")


def upgrade() -> None:
    _rebuild(" WHERE mailbox_id IS NOT NULL")


def downgrade() -> None:
    _rebuild("")

"""Test helper: which tables hold rows that belong to a user (or a mailbox)?

Derived from the migrated schema, not listed by hand, so tables added later are covered
automatically. A table "depends" on a root table if a chain of foreign keys leads from it
to the root; its rows must disappear (``ON DELETE CASCADE``) or lose the reference
(``ON DELETE SET NULL``) when the root row is deleted.
"""

from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# (child table, child column, parent table, ON DELETE action) for every foreign key.
_FOREIGN_KEYS = text(
    """
    SELECT child.relname::text, a.attname::text, parent.relname::text, c.confdeltype::text
    FROM pg_constraint c
    JOIN pg_class child ON child.oid = c.conrelid
    JOIN pg_class parent ON parent.oid = c.confrelid
    JOIN pg_namespace n ON n.oid = child.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'f' AND n.nspname = current_schema()
    """
)
# Columns that hold a user ID: every column named like this must be a foreign key.
_USER_COLUMNS = text(
    """
    SELECT table_name::text, column_name::text
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND (column_name = 'user_id' OR column_name LIKE '%\\_user\\_id')
    """
)
CASCADE, SET_NULL = "c", "n"

ForeignKey = tuple[str, str, str, str]


async def foreign_keys(session: AsyncSession) -> list[ForeignKey]:
    return [tuple(row) for row in await session.execute(_FOREIGN_KEYS)]  # type: ignore[misc]


async def user_columns(session: AsyncSession) -> set[tuple[str, str]]:
    return {(table, column) for table, column in await session.execute(_USER_COLUMNS)}


def dependent_tables(keys: list[ForeignKey], root: str) -> set[str]:
    """Tables that reference ``root`` directly or through other tables."""
    children: dict[str, set[str]] = defaultdict(set)
    for child, _, parent, _ in keys:
        children[parent].add(child)
    found: set[str] = set()
    pending = [root]
    while pending:
        for child in children[pending.pop()] - found:
            found.add(child)
            pending.append(child)
    found.discard(root)
    return found


def cascading_tables(keys: list[ForeignKey], root: str) -> set[str]:
    """Tables whose rows are deleted (by cascade) when their ``root`` row is deleted."""
    found = {root}
    changed = True
    while changed:
        changed = False
        for child, _, parent, action in keys:
            if action == CASCADE and parent in found and child not in found:
                found.add(child)
                changed = True
    found.discard(root)
    return found


def blocking_keys(keys: list[ForeignKey], root: str) -> list[ForeignKey]:
    """Foreign keys that would make deleting a ``root`` row fail (or leave a dangling
    reference): neither ``CASCADE`` nor ``SET NULL``."""
    tables = dependent_tables(keys, root) | {root}
    return [key for key in keys if key[2] in tables and key[3] not in (CASCADE, SET_NULL)]


async def row_counts(session: AsyncSession, tables: set[str]) -> dict[str, int]:
    counts = {}
    for table in sorted(tables):
        counts[table] = await session.scalar(text(f'SELECT count(*) FROM "{table}"')) or 0
    return counts

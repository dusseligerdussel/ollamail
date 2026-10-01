"""Alembic environment (async, asyncpg).

The URL comes from ``config.attributes["database_url"]`` when Alembic is driven from
code (tests), otherwise from ``OLLAMAIL_DATABASE_URL``.
"""

import asyncio
from collections.abc import Mapping
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import DatabaseSettings
from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tables managed outside the ORM (vendored SQL migrations), ignored by autogenerate.
EXTERNAL_TABLE_PREFIXES = ("procrastinate_",)


def include_name(name: str | None, type_: str, _parents: Mapping[str, str | None]) -> bool:
    return not (type_ == "table" and name is not None and name.startswith(EXTERNAL_TABLE_PREFIXES))


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if isinstance(url, str):
        return url
    return DatabaseSettings().url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        include_name=include_name,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_name=include_name
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool, hide_parameters=True)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

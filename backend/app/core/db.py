"""Database engine, sessions and the declarative base class."""

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, ClassVar

from fastapi import Request
from sqlalchemy import DateTime, MetaData, func, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.config import DatabaseSettings, get_settings
from app.core.ids import uuid7

# Deterministic constraint names keep Alembic autogenerate diffs stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models: UUIDv7 primary key plus audit timestamps.

    Tables without these columns (e.g. pure association tables) use ``sqlalchemy.Table``
    with ``Base.metadata`` instead of subclassing.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {datetime: DateTime(timezone=True)}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


def libpq_url(settings: DatabaseSettings) -> str:
    """Database URL without the SQLAlchemy driver suffix, for raw asyncpg/psycopg clients."""
    url = make_url(settings.url.get_secret_value()).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def create_engine(settings: DatabaseSettings) -> AsyncEngine:
    return create_async_engine(
        settings.url.get_secret_value(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
        # Never render bound parameters (mail data) into logs or exception messages.
        hide_parameters=True,
        connect_args={"timeout": settings.connect_timeout},
    )


class Database:
    """Owns the engine and session factory of one application instance."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.engine = create_engine(settings)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def ping(self) -> None:
        """Raise if the database cannot be reached."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()


_process_database: Database | None = None


def process_database() -> Database:
    """The one engine (and connection pool) of this process, created on first use.

    Worker tasks, the mailbox watcher and the AI settings resolver share it, so a worker
    holds at most ``pool_size + max_overflow`` SQLAlchemy connections (docs/OPERATIONS.md,
    "Datenbankverbindungen"). The api binds its own ``Database`` here (``bind_process_database``).
    Create it inside the event loop that uses it.
    """
    global _process_database
    if _process_database is None:
        _process_database = Database(get_settings().database)
    return _process_database


@contextmanager
def bind_process_database(database: Database) -> Iterator[None]:
    """Make ``database`` the process database while the block runs (api lifespan, tests)."""
    global _process_database
    saved, _process_database = _process_database, database
    try:
        yield
    finally:
        _process_database = saved


async def dispose_process_database() -> None:
    """Close the pool of the process database, if one was created (worker shutdown)."""
    global _process_database
    database, _process_database = _process_database, None
    if database is not None:
        await database.dispose()


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session; callers commit explicitly."""
    database: Database = request.app.state.database
    async with database.sessionmaker() as session:
        yield session

"""Fixtures for mail tests. All fixture mails are synthetic (example.* domains)."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import DatabaseSettings
from app.core.db import libpq_url
from app.mail import hooks
from app.mail.models import Mailbox, MailboxType
from app.mail.storage import AttachmentStorage
from app.users.models import User
from tests.factories import make_user
from tests.mail import imap_server

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_names() -> list[str]:
    return sorted(path.name for path in FIXTURES.glob("*.eml"))


@pytest.fixture
def storage(tmp_path: Path) -> AttachmentStorage:
    return AttachmentStorage(tmp_path)


@pytest.fixture(autouse=True)
def isolated_hooks() -> Iterator[list[hooks.MessageStoredHandler]]:
    """Mail tests run without the handlers other modules registered (e.g. the processing
    pipeline, which would queue jobs); yields the registered handlers."""
    registered = list(hooks._handlers)
    hooks._handlers.clear()
    yield registered
    hooks._handlers[:] = registered


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``imap`` tests if the IMAP test server is unreachable (see ``imap_server``)."""
    imap_items = [item for item in items if item.get_closest_marker("imap")]
    if not imap_items:
        return
    error = imap_server.probe()
    if error is None:
        return
    message = (
        f"IMAP test server not reachable at {imap_server.HOST}:{imap_server.PORT} ({error}). "
        "See tests/mail/imap_server.py."
    )
    if imap_server.REQUIRE:
        raise pytest.UsageError(message)
    for item in imap_items:
        item.add_marker(pytest.mark.skip(reason=message))


@pytest.fixture
async def imap_account() -> AsyncIterator[imap_server.TestAccount]:
    account = imap_server.TestAccount()
    try:
        yield account
    finally:
        await account.close()


@pytest.fixture
async def sessionmaker(migrated_database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on committed data (for code that opens its own sessions). Mailboxes created
    with ``add_mailbox`` and their owners are deleted afterwards, with everything that
    cascades from them."""
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owners: list[uuid.UUID] = []
    factory.owners = owners  # type: ignore[attr-defined]
    yield factory
    async with factory() as session:
        await session.execute(delete(User).where(User.id.in_(owners)))
        await session.commit()
    await engine.dispose()


async def add_mailbox(factory: async_sessionmaker[AsyncSession], **values: Any) -> uuid.UUID:
    async with factory() as session:
        owner = await make_user(session)
        factory.owners.append(owner.id)  # type: ignore[attr-defined]
        defaults: dict[str, Any] = {
            "type": MailboxType.IMAP,
            "display_name": "Watched",
            "address": "erika@example.org",
            "owner_user_id": owner.id,
        }
        mailbox = Mailbox(**{**defaults, **values})
        session.add(mailbox)
        await session.commit()
        return mailbox.id


async def eventually(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def dsn(url: str) -> str:
    return libpq_url(DatabaseSettings.model_validate({"url": url}))

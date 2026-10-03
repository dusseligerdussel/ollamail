"""Removing a large mailbox and the IMAP flag check without CONDSTORE (#147), 100k messages.

Checks what does not depend on the machine (no mail deleted in the request, batch sizes,
statements per flag check); timings are printed for the PR (``pytest -s``). Skipped unless
``OLLAMAIL_TEST_PERF=1`` (set in CI).
"""

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.mail import deletion
from app.mail.models import Mailbox, Message
from app.mail.providers.base import FlagsReported, MessageUpdated, SyncEvent
from app.mail.service import delete_mailbox
from app.mail.storage import AttachmentStorage
from app.mail.sync import engine
from tests.mail.test_sync_changes import ScriptedProvider, run
from tests.perf.seed import seed_large_mailbox

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get("OLLAMAIL_TEST_PERF", "").lower() not in {"1", "true", "yes"},
        reason="performance test; set OLLAMAIL_TEST_PERF=1",
    ),
]

MESSAGES = 100_000


class Statements:
    """Statements the session runs while recording (the SQL text only)."""

    def __init__(self, session: AsyncSession) -> None:
        self.engine = session.bind.engine.sync_engine  # type: ignore[union-attr]
        self.recorded: list[str] = []

    def _record(self, _conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        self.recorded.append(" ".join(statement.split()))

    def __enter__(self) -> "Statements":
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def starting(self, prefix: str) -> list[str]:
        return [s for s in self.recorded if s.upper().startswith(prefix)]


@pytest.fixture
def storage(tmp_path: Any) -> AttachmentStorage:
    return AttachmentStorage(tmp_path)


@pytest.fixture(autouse=True)
def _no_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def publish(*args: Any) -> None:
        return None

    monkeypatch.setattr(engine, "publish", publish)
    monkeypatch.setattr(deletion, "publish", publish)
    yield


async def _count(session: AsyncSession, mailbox_id: Any) -> int:
    query = select(func.count()).select_from(Message).where(Message.mailbox_id == mailbox_id)
    return await session.scalar(query) or 0


async def test_mailbox_removal_runs_in_batches(
    db_session: AsyncSession, storage: AttachmentStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Before #147: one cascading statement inside the request.
    before = await seed_large_mailbox(db_session, MESSAGES)
    await db_session.commit()
    started = time.perf_counter()
    await delete_mailbox(db_session, before.account.mailbox.id, storage)
    single = time.perf_counter() - started

    large = await seed_large_mailbox(db_session, MESSAGES)
    mailbox = large.account.mailbox
    await db_session.commit()

    # The request only marks the mailbox.
    with Statements(db_session) as statements:
        started = time.perf_counter()
        await deletion.request_deletion(db_session, mailbox, audit.SYSTEM)
        await db_session.commit()
        request = time.perf_counter() - started
    assert not [s for s in statements.starting("DELETE") if "mail_messages" in s]
    assert await _count(db_session, mailbox.id) == MESSAGES

    # The job: one transaction per batch, none of them long.
    batches: list[float] = []
    commit = db_session.commit

    async def timed_commit() -> None:
        await commit()
        batches.append(time.perf_counter())

    monkeypatch.setattr(db_session, "commit", timed_commit)
    with Statements(db_session) as statements:
        started = time.perf_counter()
        result = await deletion.purge_mailbox(db_session, mailbox.id, storage)
        purge = time.perf_counter() - started
    deletes = [s for s in statements.starting("DELETE FROM MAIL_MESSAGES")]
    assert result.messages == MESSAGES and result.deleted
    assert len(deletes) == MESSAGES // deletion.MESSAGE_BATCH
    assert await db_session.get(Mailbox, mailbox.id) is None
    longest = max(b - a for a, b in zip([started, *batches], batches, strict=False))
    print(
        f"\nremoval of {MESSAGES} mails: single statement {single:.2f} s;"
        f" request now {request * 1000:.1f} ms, job {purge:.2f} s"
        f" in {len(batches)} transactions, longest {longest * 1000:.0f} ms"
    )


async def test_flag_check_compares_in_bulk(
    db_session: AsyncSession, storage: AttachmentStorage
) -> None:
    large = await seed_large_mailbox(db_session, MESSAGES)
    mailbox_id = large.account.mailbox.id
    await db_session.commit()
    rows = (
        await db_session.execute(
            select(Message.remote_ref, Message.flags).where(Message.mailbox_id == mailbox_id)
        )
    ).all()
    # The server reports every message; 1 % of them changed.
    current = {
        ref: frozenset(flags) ^ ({"flagged"} if index % 100 == 0 else set())
        for index, (ref, flags) in enumerate(rows)
    }

    async def sync(events: list[SyncEvent]) -> tuple[float, int, int]:
        provider = ScriptedProvider({"INBOX": events})
        with Statements(db_session) as statements:
            started = time.perf_counter()
            stats = await run(db_session, mailbox_id, lambda config: provider, storage)
            elapsed = time.perf_counter() - started
        assert stats is not None
        return elapsed, len(statements.recorded), stats.updated

    # Before #147: one event per message.
    single, single_statements, updated = await sync(
        [MessageUpdated(ref, flags=flags) for ref, flags in current.items()]
    )
    assert updated == MESSAGES // 100
    # Now: bulk reports; flip the same messages back.
    flipped = {
        ref: frozenset(flags) for ref, flags in zip(current, (f for _, f in rows), strict=True)
    }
    refs = list(flipped.items())
    size = 10_000
    reports: list[SyncEvent] = [
        FlagsReported(dict(refs[start : start + size])) for start in range(0, len(refs), size)
    ]
    bulk, bulk_statements, updated = await sync(reports)
    assert updated == MESSAGES // 100
    # A handful of statements per report instead of a few per 500 messages.
    assert bulk_statements < single_statements / 10
    print(
        f"\nflag check of {MESSAGES} mails (1 % changed): per message {single:.2f} s,"
        f" {single_statements} statements; bulk {bulk:.2f} s, {bulk_statements} statements"
    )

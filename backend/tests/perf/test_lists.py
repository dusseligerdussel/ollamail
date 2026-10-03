"""Inbox and triage lists with 100k synthetic messages (#140).

Checks the query plans, not wall-clock times (those depend on the machine): every page reads
the index ``ix_mail_messages_mailbox_id_sort_date_id`` in list order and stops after about
one page of rows, wherever the page is in the list. Only the first page counts all messages.
Timings are printed for the PR (``pytest -s``).

Takes about half a minute; skipped unless ``OLLAMAIL_TEST_PERF=1`` (set in CI).
"""

import json
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.perf.seed import LargeMailbox, seed_large_mailbox
from tests.triage.conftest import account_for

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get("OLLAMAIL_TEST_PERF", "").lower() not in {"1", "true", "yes"},
        reason="performance test; set OLLAMAIL_TEST_PERF=1",
    ),
]

MESSAGES = 100_000
INDEX = "ix_mail_messages_mailbox_id_sort_date_id"
# Rows a page may read from ``mail_messages``: one page plus what the filters skip
# (5 % archive, segment boundaries), far below the 100k of a full scan.
MAX_ROWS_PER_PAGE = 5_000


@pytest.fixture
async def large(db_client: AsyncClient, db_session: AsyncSession) -> LargeMailbox:
    user = await make_local_user(db_session, "perf@example.org")
    assert (await login(db_client, user.email)).status_code == 200
    return await seed_large_mailbox(db_session, MESSAGES, await account_for(db_session, user))


class Statements:
    """SQL statements (with parameters) run by the session while recording."""

    def __init__(self, session: AsyncSession) -> None:
        self.engine = session.bind.engine.sync_engine  # type: ignore[union-attr]
        self.recorded: list[tuple[str, Any]] = []

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "mail_messages" in statement:
            self.recorded.append((statement, parameters))

    def __enter__(self) -> "Statements":
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *_: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)


def _nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


async def _plans(session: AsyncSession, statements: Statements) -> list[dict[str, Any]]:
    connection = await session.connection()
    plans = []
    for statement, parameters in statements.recorded:
        result = await connection.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters
        )
        raw = result.scalar_one()
        plans.append((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"])
    return plans


async def _page(
    client: AsyncClient, session: AsyncSession, path: str, params: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], float]:
    with Statements(session) as statements:
        started = time.perf_counter()
        response = await client.get(path, params=params)
        elapsed = (time.perf_counter() - started) * 1000
    assert response.status_code == 200, response.text
    return response.json(), await _plans(session, statements), elapsed


def _rows_read(plan: dict[str, Any]) -> int:
    """Rows the plan read from ``mail_messages`` (all scans of the table)."""
    return sum(
        int(node.get("Actual Rows", 0) * node.get("Actual Loops", 1))
        for node in _nodes(plan)
        if node.get("Relation Name") == "mail_messages"
    )


def _assert_keyset_page(plans: list[dict[str, Any]]) -> None:
    """The page rows come from the list index, in its order, without reading everything."""
    pages = [p for p in plans if p["Node Type"] == "Limit"]
    assert pages
    for plan in pages:
        scans = [n for n in _nodes(plan) if n.get("Relation Name") == "mail_messages"]
        assert {n.get("Index Name") for n in scans} == {INDEX}, scans
        assert not any(n["Node Type"] == "Sort" for n in _nodes(plan))
        assert _rows_read(plan) <= MAX_ROWS_PER_PAGE


async def _walk(
    client: AsyncClient, session: AsyncSession, path: str, pages: int, **params: Any
) -> None:
    """Load ``pages`` pages; check the first (with counts) and the last (deep keyset page)."""
    cursor = None
    for number in range(1, pages + 1):
        query = {**params, "limit": 100} | ({"cursor": cursor} if cursor else {})
        body, plans, elapsed = await _page(client, session, path, query)
        if number in (1, pages):
            print(f"{path} {params} page {number}: {elapsed:.1f} ms")
            _assert_keyset_page(plans)
            assert (body["total"] is None) == (number > 1)
        assert len(body["items"]) == 100
        cursor = body["next_cursor"]


async def test_inbox_pages_read_the_index(
    db_client: AsyncClient, db_session: AsyncSession, large: LargeMailbox
) -> None:
    await _walk(db_client, db_session, "/messages", 51)
    await _walk(db_client, db_session, "/messages", 2, unread=True)
    await _walk(db_client, db_session, "/messages", 2, mailbox_id=str(large.account.mailbox.id))


async def test_triage_pages_read_the_index(
    db_client: AsyncClient, db_session: AsyncSession, large: LargeMailbox
) -> None:
    await _walk(db_client, db_session, "/triage/inbox/messages", 51)
    await _walk(db_client, db_session, "/triage/inbox/messages", 2, category="none")
    body = (await db_client.get("/triage/inbox/messages")).json()
    newsletter = body["groups"][4]["category_id"]
    await _walk(db_client, db_session, "/triage/inbox/messages", 2, category=newsletter)

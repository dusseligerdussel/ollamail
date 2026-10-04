"""Inbox and triage lists with 100k synthetic messages (#140, #186).

Checks the query plans, not wall-clock times (those depend on the machine): every page reads
its rows from an index in list order and stops after about one page of rows, wherever the
page is in the list, also for rare filters (few unread mails, small triage segments) in an
archive-heavy mailbox. Only the first page counts, and the counts read the inbox folder,
not the mailbox. Timings are printed for the PR (``pytest -s``).

Takes about a minute; skipped unless ``OLLAMAIL_TEST_PERF=1`` (set in CI).
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
# Indexes a page may read ``mail_messages`` and ``triage_results`` with: the lists in their
# order, and single rows by key.
PAGE_INDEXES = {
    "ix_mail_messages_mailbox_id_sort_date_id",
    "ix_mail_messages_unread",
    "ix_triage_results_segment",
    "pk_mail_messages",
    "uq_triage_results_message_id",
}
LIST_TABLES = {"mail_messages", "triage_results"}
# Rows a page may fetch from ``mail_messages``: one page plus what the filters skip
# (archive, segment boundaries), far below the 100k of a full scan.
MAX_ROWS_PER_PAGE = 5_000
# Entries a page may read from an index range without fetching messages: a triage segment
# (joined with the unread messages) or the unread index. A part of the mailbox, never all.
MAX_RANGE_ROWS_PER_PAGE = MESSAGES // 5


@pytest.fixture
async def large(db_client: AsyncClient, db_session: AsyncSession) -> LargeMailbox:
    user = await make_local_user(db_session, "perf@example.org")
    assert (await login(db_client, user.email)).status_code == 200
    return await seed_large_mailbox(db_session, MESSAGES, await account_for(db_session, user))


@pytest.fixture
async def skewed(db_client: AsyncClient, db_session: AsyncSession) -> LargeMailbox:
    user = await make_local_user(db_session, "perf@example.org")
    assert (await login(db_client, user.email)).status_code == 200
    account = await account_for(db_session, user)
    return await seed_large_mailbox(db_session, MESSAGES, account, skewed=True)


class Statements:
    """SQL statements (with parameters) run by the session while recording."""

    def __init__(self, session: AsyncSession) -> None:
        self.engine = session.bind.engine.sync_engine  # type: ignore[union-attr]
        self.recorded: list[tuple[str, Any]] = []

    def _record(self, _conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if statement.lstrip().upper().startswith(("SELECT", "WITH")) and (
            "mail_message" in statement or "triage_results" in statement
        ):
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


def _rows_read(plan: dict[str, Any], *, messages: bool) -> int:
    """Rows the plan fetched from ``mail_messages`` (``messages``; all scans but index-only
    ones, with the rows a filter dropped), or entries it read from index ranges otherwise
    (``triage_results`` and index-only scans of ``mail_messages``)."""

    def fetches(node: dict[str, Any]) -> bool:
        return (
            node.get("Relation Name") == "mail_messages" and node["Node Type"] != "Index Only Scan"
        )

    return sum(
        int(
            (node.get("Actual Rows", 0) + node.get("Rows Removed by Filter", 0))
            * node.get("Actual Loops", 1)
        )
        for node in _nodes(plan)
        if node.get("Relation Name") in LIST_TABLES and fetches(node) == messages
    )


def _assert_keyset_page(plans: list[dict[str, Any]], max_fetched: int = MAX_ROWS_PER_PAGE) -> None:
    """The page rows come from the list indexes, in their order, without reading everything;
    the counts (first page only) read the folders, never all messages of the mailbox."""
    pages = [p for p in plans if p["Node Type"] == "Limit"]
    assert pages
    for plan in pages:
        scans = [n for n in _nodes(plan) if n.get("Relation Name") in LIST_TABLES]
        assert {n.get("Index Name") for n in scans} <= PAGE_INDEXES, scans
        # Merging the parts of several mailboxes or categories sorts a few pages of rows.
        assert all(n["Actual Rows"] <= 1_000 for n in _nodes(plan) if n["Node Type"] == "Sort")
        assert _rows_read(plan, messages=True) <= max_fetched
        assert _rows_read(plan, messages=False) <= MAX_RANGE_ROWS_PER_PAGE
    for plan in plans:
        if plan["Node Type"] != "Limit":
            assert all(
                n["Node Type"] != "Seq Scan"
                for n in _nodes(plan)
                if n.get("Relation Name") == "mail_messages"
            )


async def _walk(
    client: AsyncClient,
    session: AsyncSession,
    path: str,
    pages: int,
    max_fetched: int = MAX_ROWS_PER_PAGE,
    **params: Any,
) -> int:
    """Load up to ``pages`` pages; check the first (with counts) and the last (deep keyset
    page). Returns the number of messages loaded."""
    cursor = None
    loaded = 0
    for number in range(1, pages + 1):
        query = {**params, "limit": 100} | ({"cursor": cursor} if cursor else {})
        body, plans, elapsed = await _page(client, session, path, query)
        cursor = body["next_cursor"]
        if number in (1, pages) or cursor is None:
            print(f"{path} {params} page {number}: {elapsed:.1f} ms")
            _assert_keyset_page(plans, max_fetched)
            assert (body["total"] is None) == (number > 1)
        loaded += len(body["items"])
        assert len(body["items"]) == 100 or cursor is None
        if cursor is None:
            break
    return loaded


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


async def test_rare_filters_read_only_their_rows(
    db_client: AsyncClient, db_session: AsyncSession, skewed: LargeMailbox
) -> None:
    """Archive-heavy mailbox with few unread mails and small triage segments (#186): a
    page reads about the rows it shows, not the mailbox."""
    unread = await _walk(db_client, db_session, "/messages", 5, unread=True)
    _, plans, _ = await _page(db_client, db_session, "/messages", {"unread": True})
    page = next(p for p in plans if p["Node Type"] == "Limit")
    assert "ix_mail_messages_unread" in {n.get("Index Name") for n in _nodes(page)}
    assert unread == (await db_client.get("/messages", params={"unread": True})).json()["total"]
    archive = str(skewed.archive.id)
    await _walk(db_client, db_session, "/messages", 2, folder_id=archive)

    path = "/triage/inbox/messages"
    await _walk(db_client, db_session, path, 51)
    # Unread in a triage segment: depending on its estimates the planner starts from the
    # unread messages or walks the segment and checks each message; either is bounded by
    # the segment, not the mailbox.
    await _walk(db_client, db_session, path, 5, MAX_RANGE_ROWS_PER_PAGE, unread=True)
    groups = (await db_client.get(path)).json()["groups"]
    important, spam, newsletter = (groups[i]["category_id"] for i in (0, 6, 4))
    for category in (important, spam, "none"):
        await _walk(db_client, db_session, path, 5, category=category)
    # A hidden category joins the uncategorised messages, one index range per category.
    hidden = await db_client.patch(f"/triage/categories/{newsletter}", json={"hidden": True})
    assert hidden.status_code == 200
    await _walk(db_client, db_session, path, 3, category="none")

"""Integration tests: classic search API with sign-in, PostgreSQL and a fake embedder."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.search.router import excerpt, get_embedder
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.rag.conftest import Inbox, inbox  # noqa: F401
from tests.search.conftest import FakeEmbedder

pytestmark = pytest.mark.db

FLIGHT = "Your flight to Lisbon boards at 09:40 at gate B12."
INVOICE = "Please find attached the invoice for September, payable within 14 days."


@pytest.fixture
async def http(
    settings: Settings,
    db_session: AsyncSession,
    embedder: FakeEmbedder,
    search_settings: Settings,
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings.model_copy(update={"search": search_settings}))

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_embedder] = lambda: embedder
    async with api_client(app) as client:
        yield client
    await app.state.database.dispose()


async def signed_in(http: AsyncClient, session: AsyncSession, email: str) -> User:
    user = await make_local_user(session, email)
    assert (await login(http, email)).status_code == 200
    return user


async def test_requires_sign_in(http: AsyncClient) -> None:
    assert (await http.post("/search", json={"query": "flight"})).status_code == 401


async def test_hits_with_message_fields_and_excerpt(
    http: AsyncClient,
    db_session: AsyncSession,
    inbox: Inbox,  # noqa: F811
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    mailbox = await inbox.mail.mailbox(user.id)
    flight = await inbox.add(
        mailbox,
        FLIGHT,
        subject="Booking",
        sender={"name": "Air Example", "address": "noreply@air.example"},
        sent_at=datetime(2026, 9, 1, 8, tzinfo=UTC),
    )
    await inbox.add(mailbox, INVOICE, subject="Invoice")

    response = await http.post("/search", json={"query": "gate"})

    assert response.status_code == 200, response.text
    hits = response.json()["hits"]
    # The full-text match ranks first (vector neighbours may follow).
    hit = hits[0]
    assert hit["message_id"] == str(flight)
    assert hit["subject"] == "Booking"
    assert hit["sender"] == {"name": "Air Example", "address": "noreply@air.example"}
    assert hit["date"].startswith("2026-09-01T08:00:00")
    assert hit["source"] == "body"
    assert "gate B12" in hit["excerpt"]


async def test_semantic_hits_and_one_hit_per_message(
    http: AsyncClient,
    db_session: AsyncSession,
    inbox: Inbox,  # noqa: F811
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    mailbox = await inbox.mail.mailbox(user.id)
    invoice = await inbox.add(mailbox, f"{INVOICE}\n\n" + "Payment details follow. " * 40)

    hits = (await http.post("/search", json={"query": "billing"})).json()["hits"]

    # Found by meaning ("billing" ~ "invoice", "payment"), once despite several chunks.
    assert [hit["message_id"] for hit in hits] == [str(invoice)]
    assert hits[0]["source"] == "body"


async def test_filters_narrow_the_hits(
    http: AsyncClient,
    db_session: AsyncSession,
    inbox: Inbox,  # noqa: F811
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    work = await inbox.mail.mailbox(user.id)
    private = await inbox.mail.mailbox(user.id)
    old = await inbox.add(
        work,
        FLIGHT,
        sender={"name": "Air Example", "address": "noreply@air.example"},
        sent_at=datetime(2026, 1, 5, tzinfo=UTC),
    )
    recent = await inbox.add(
        private,
        FLIGHT,
        sender={"name": "Travel Agency", "address": "desk@travel.example"},
        sent_at=datetime(2026, 9, 5, tzinfo=UTC),
    )

    async def found(filters: dict[str, object]) -> set[str]:
        response = await http.post("/search", json={"query": "flight", "filters": filters})
        assert response.status_code == 200, response.text
        return {hit["message_id"] for hit in response.json()["hits"]}

    assert await found({}) == {str(old), str(recent)}
    assert await found({"mailbox_ids": [str(work)]}) == {str(old)}
    assert await found({"sender": "travel"}) == {str(recent)}
    assert await found({"since": "2026-06-01T00:00:00Z"}) == {str(recent)}
    assert await found({"until": "2026-06-01T00:00:00Z"}) == {str(old)}


async def test_users_only_find_their_own_mail(
    http: AsyncClient,
    db_session: AsyncSession,
    inbox: Inbox,  # noqa: F811
) -> None:
    bob = await make_local_user(db_session, "bob@example.org")
    bob_box = await inbox.mail.mailbox(bob.id)
    await inbox.add(bob_box, "Bob's secret: the merger closes on Monday.")
    shared = await inbox.mail.mailbox(None)
    await inbox.add(shared, "Shared mailbox: the merger is confidential.")
    await signed_in(http, db_session, "erika@example.org")

    assert (await http.post("/search", json={"query": "merger"})).json()["hits"] == []
    response = await http.post(
        "/search", json={"query": "merger", "filters": {"mailbox_ids": [str(bob_box)]}}
    )
    assert response.json()["hits"] == []


async def test_request_is_validated(http: AsyncClient, db_session: AsyncSession) -> None:
    await signed_in(http, db_session, "erika@example.org")

    assert (await http.post("/search", json={"query": "  "})).status_code == 422
    assert (await http.post("/search", json={"query": "x" * 501})).status_code == 422
    response = await http.post("/search", json={"query": "x", "filters": {"owner": "bob"}})
    assert response.status_code == 422
    assert (await http.post("/search", json={"query": "x", "limit": 0})).status_code == 422


def test_excerpt_centres_on_the_first_term() -> None:
    text = " ".join(f"word{n}" for n in range(200)) + " the Invoice number 42 " + "tail " * 100

    result = excerpt(text, "invoice", length=80)

    assert "Invoice number 42" in result
    assert result.startswith("… ") and result.endswith(" …")
    assert len(result) <= 80 + 4
    assert excerpt("short   text\n here", "x") == "short text here"
    assert excerpt("a " * 100, "missing", length=20).startswith("a a")

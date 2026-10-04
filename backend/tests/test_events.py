import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Header
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import DatabaseSettings, SecuritySettings, Settings
from app.core.crypto import generate_key
from app.core.current_user import get_current_user_id
from app.core.events import (
    Event,
    EventBroker,
    EventEnvelope,
    Subscription,
    _stream,
    get_session_check,
    publish,
)
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL

ALICE = UUID("0190a000-0000-7000-8000-00000000000a")
BOB = UUID("0190a000-0000-7000-8000-00000000000b")
MAILBOX = UUID("0190a000-0000-7000-8000-0000000000aa")


def _broker() -> EventBroker:
    return EventBroker(DatabaseSettings.model_validate({"url": TEST_DATABASE_URL}))


def test_event_accepts_ids_and_status() -> None:
    event = Event(type="mailbox.sync", ids={"mailbox_id": MAILBOX, "count_id": 3}, status="done")

    assert json.loads(event.model_dump_json()) == {
        "type": "mailbox.sync",
        "ids": {"mailbox_id": str(MAILBOX), "count_id": 3},
        "status": "done",
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"type": "Quarterly numbers"},
        {"type": "mail.new", "status": "Hello Bob, see attached"},
        {"type": "mail.new", "ids": {"subject": MAILBOX}},
        {"type": "mail.new", "ids": {"message_id": "alice@example.com"}},
        {"type": "mail.new", "subject": "Quarterly numbers"},
    ],
)
def test_event_rejects_content(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Event(**fields)


def test_dispatch_delivers_only_to_the_events_user() -> None:
    broker = _broker()
    event = Event(type="mailbox.sync", ids={"mailbox_id": MAILBOX}, status="running")

    with broker.subscribe(ALICE) as alice, broker.subscribe(BOB) as bob:
        broker.dispatch(EventEnvelope(user_id=ALICE, event=event).model_dump_json())
        broker.dispatch("not json")

        assert alice.queue.get_nowait() == event
        assert alice.queue.empty()
        assert bob.queue.empty()


def test_subscription_is_released() -> None:
    broker = _broker()
    event = EventEnvelope(user_id=ALICE, event=Event(type="ping"))

    with broker.subscribe(ALICE) as subscription:
        pass
    broker.dispatch(event.model_dump_json())

    assert subscription.queue.empty()
    assert broker._subscriptions == {}


async def test_events_require_authentication(client: AsyncClient) -> None:
    # Until #11 every request is unauthenticated.
    response = await client.get("/events")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_events_endpoint_is_documented(settings: Settings) -> None:
    schema = create_app(settings).openapi()

    assert "text/event-stream" in schema["paths"]["/events"]["get"]["responses"]["200"]["content"]


# --- Integration tests against PostgreSQL -------------------------------------------------


async def _test_user(x_test_user: Annotated[UUID, Header()]) -> UUID:
    """Stand-in for authentication (#11): the user ID comes from a test header."""
    return x_test_user


async def _always_valid() -> bool:
    return True


def _test_auth(app: FastAPI) -> None:
    app.dependency_overrides[get_current_user_id] = _test_user
    app.dependency_overrides[get_session_check] = lambda: _always_valid


@pytest.fixture
async def server(settings: Settings, migrated_database: str) -> AsyncIterator[str]:
    """The app served by a real uvicorn on a free port (ASGITransport cannot stream)."""
    # The real lifespan runs here, and it refuses to start without a master key.
    security = SecuritySettings(secret_key=SecretStr(generate_key()))
    app: FastAPI = create_app(settings.model_copy(update={"security": security}))
    _test_auth(app)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        # Open SSE streams never finish on their own.
        server.force_exit = True
        await task
        sock.close()


async def _events(lines: AsyncIterator[str], count: int) -> list[dict[str, Any]]:
    """Read ``count`` SSE events, skipping comments and control fields."""
    received: list[dict[str, Any]] = []
    async for line in lines:
        if line.startswith("data: "):
            received.append(json.loads(line.removeprefix("data: ")))
            if len(received) == count:
                break
    return received


async def _until_ready(lines: AsyncIterator[str]) -> None:
    async for line in lines:
        if line.startswith("retry: "):
            return


async def _publish(user_id: UUID, event: Event, *, commit: bool = True) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as connection:
        await publish(connection, user_id, event)
        if commit:
            await connection.commit()
        else:
            await connection.rollback()
    await engine.dispose()


@pytest.mark.db
async def test_published_event_reaches_only_its_user(server: str) -> None:
    for_alice = Event(type="mailbox.sync", ids={"mailbox_id": MAILBOX}, status="done")
    for_bob = Event(type="mailbox.sync", ids={"mailbox_id": MAILBOX}, status="failed")
    rolled_back = Event(type="mailbox.sync", status="running")

    async with (
        AsyncClient(base_url=server, trust_env=False, timeout=10) as http,
        http.stream("GET", "/events", headers={"X-Test-User": str(ALICE)}) as alice,
        http.stream("GET", "/events", headers={"X-Test-User": str(BOB)}) as bob,
    ):
        assert alice.status_code == 200
        assert alice.headers["content-type"].startswith("text/event-stream")
        alice_lines, bob_lines = alice.aiter_lines(), bob.aiter_lines()
        await _until_ready(alice_lines)
        await _until_ready(bob_lines)

        # Rolled back: never delivered, Alice's first event is the committed one.
        await _publish(ALICE, rolled_back, commit=False)
        await _publish(ALICE, for_alice)
        await _publish(BOB, for_bob)

        async with asyncio.timeout(10):
            alice_events = await _events(alice_lines, 1)
            # Bob's first event is his own: Alice's events were never sent to him.
            bob_events = await _events(bob_lines, 1)

    assert alice_events == [json.loads(for_alice.model_dump_json())]
    assert bob_events == [json.loads(for_bob.model_dump_json())]


async def test_events_unavailable_without_database(settings: Settings) -> None:
    unreachable = DatabaseSettings.model_validate(
        {"url": "postgresql+asyncpg://u:p@127.0.0.1:1/x", "connect_timeout": 0.2}
    )
    app = create_app(settings.model_copy(update={"database": unreachable}))
    _test_auth(app)
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/events", headers={"X-Test-User": str(ALICE)})
    await app.state.events.stop()

    assert response.status_code == 503
    assert app.state.events.stream_count(ALICE) == 0


# --- Limits (#191) ------------------------------------------------------------------------


async def _lines(stream: AsyncIterator[str], count: int) -> list[str]:
    return [await anext(stream) for _ in range(count)]


async def test_stream_ends_when_the_session_is_no_longer_valid() -> None:
    subscription = Subscription(ALICE)
    results = iter([True, False])
    checks = 0

    async def check() -> bool:
        nonlocal checks
        checks += 1
        return next(results)

    subscription.queue.put_nowait(Event(type="ping"))
    stream = _stream(subscription, check, 0.05)
    async with asyncio.timeout(5):
        first, event = await _lines(stream, 2)
        assert first.startswith("retry: ")
        assert event.startswith("event: ping")
        remaining = [line async for line in stream]

    # Keep-alives while the session is valid, then the stream ends.
    assert remaining and all(line == ": keep-alive\n\n" for line in remaining)
    assert checks == 2


async def test_stream_ends_when_the_session_check_fails() -> None:
    async def check() -> bool:
        raise OSError("database unreachable")

    stream = _stream(Subscription(ALICE), check, 0.05)
    async with asyncio.timeout(5):
        lines = [line async for line in stream]

    assert len(lines) == 1 and lines[0].startswith("retry: ")


async def test_streams_per_user_are_limited(settings: Settings) -> None:
    limited = settings.model_copy(
        update={"events": settings.events.model_copy(update={"max_streams_per_user": 2})}
    )
    app = create_app(limited)
    _test_auth(app)
    broker: EventBroker = app.state.events
    transport = httpx.ASGITransport(app=app)
    with broker.subscribe(ALICE), broker.subscribe(ALICE):
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/events", headers={"X-Test-User": str(ALICE)})
    await app.state.database.dispose()

    assert response.status_code == 429
    assert response.json()["error_code"] == "too_many_streams"
    assert response.headers["retry-after"].isdigit()
    # The refused request left no subscription behind.
    assert broker.stream_count(ALICE) == 0

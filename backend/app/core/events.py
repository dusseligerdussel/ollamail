"""Real-time events for the UI: PostgreSQL ``LISTEN``/``NOTIFY`` → Server-Sent Events.

Producers (API handlers, worker tasks) call ``publish(connection, user_id, event)``. The
notification is sent when the surrounding transaction commits, so the UI never hears about
changes that were rolled back. Every API process keeps one listening connection
(``EventBroker``) and fans notifications out to the SSE streams of ``GET /events``, each of
which only receives the events of its authenticated user.

Privacy: an ``Event`` carries a type, resource IDs and a status, nothing else. The fields
are validated against identifier patterns so free text (subjects, names, addresses) cannot
slip in. Clients fetch details through the regular, access-controlled API.

Delivery is best effort: events published while a client is disconnected (or while the
listener reconnects) are lost. Clients refetch their data after (re)connecting.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.config import DatabaseSettings
from app.core.current_user import get_current_user_id
from app.core.db import libpq_url
from app.core.errors import ProblemError
from app.core.logging import get_logger

CHANNEL = "ollamail_events"

# Seconds between SSE comments that keep proxies from closing idle streams.
HEARTBEAT_INTERVAL = 15.0
# Milliseconds the browser waits before reconnecting (SSE ``retry`` field).
RECONNECT_DELAY_MS = 5000
# Events buffered per stream; a client that falls further behind loses events.
STREAM_BUFFER = 100

log = get_logger(__name__)

_NAME = r"^[a-z][a-z0-9_]*$"

EventType = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", max_length=64)
]
IdKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*_id$", max_length=64)]
Status = Annotated[str, StringConstraints(pattern=_NAME, max_length=32)]


class Event(BaseModel):
    """A change notification: what happened to which resources. No content."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Dotted name, e.g. ``message.processed`` or ``mailbox.sync``.
    type: EventType
    # Affected resources, e.g. ``{"mailbox_id": ..., "message_id": ...}``.
    ids: dict[IdKey, UUID | int] = Field(default_factory=dict, max_length=8)
    # Machine-readable state, e.g. ``running``, ``done``, ``failed``.
    status: Status | None = None


class EventEnvelope(BaseModel):
    """Wire format of a notification on ``CHANNEL``."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    event: Event


async def publish(connection: AsyncSession | AsyncConnection, user_id: UUID, event: Event) -> None:
    """Queue ``event`` for ``user_id``; it is delivered when the transaction commits."""
    payload = EventEnvelope(user_id=user_id, event=event).model_dump_json()
    await connection.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": payload},
    )


class Subscription:
    """Events for one user, buffered for one SSE stream."""

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=STREAM_BUFFER)


class EventBroker:
    """One listening connection per process, fanning events out to subscriptions.

    The connection is opened on the first subscription and re-established with backoff
    if it is lost.
    """

    def __init__(self, settings: DatabaseSettings, *, keepalive: float = 30.0) -> None:
        self._dsn = libpq_url(settings)
        self._connect_timeout = settings.connect_timeout
        self._keepalive = keepalive
        self._subscriptions: dict[UUID, set[Subscription]] = {}
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def wait_ready(self) -> None:
        """Start the listener if needed; raises ``TimeoutError`` if it cannot connect."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen_forever(), name="event-listener")
        async with asyncio.timeout(self._connect_timeout + 1):
            await self._ready.wait()

    @contextmanager
    def subscribe(self, user_id: UUID) -> Iterator[Subscription]:
        """Receive the events of ``user_id`` while the context is open."""
        subscription = Subscription(user_id)
        self._subscriptions.setdefault(user_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            self._release(subscription)

    def _release(self, subscription: Subscription) -> None:
        subscriptions = self._subscriptions.get(subscription.user_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            del self._subscriptions[subscription.user_id]

    def dispatch(self, payload: str) -> None:
        """Deliver one raw notification to the subscriptions of its user."""
        try:
            envelope = EventEnvelope.model_validate_json(payload)
        except ValidationError:
            log.warning("event_invalid")
            return
        for subscription in self._subscriptions.get(envelope.user_id, ()):
            try:
                subscription.queue.put_nowait(envelope.event)
            except asyncio.QueueFull:
                log.warning("event_dropped", event_type=envelope.event.type)

    def _on_notification(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        self.dispatch(payload)

    async def _listen_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._listen_once()
                backoff = 1.0
            except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
                log.warning("event_listener_failed", error_type=type(exc).__name__)
            finally:
                self._ready.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _listen_once(self) -> None:
        connection = await asyncpg.connect(self._dsn, timeout=self._connect_timeout)
        lost = asyncio.Event()
        connection.add_termination_listener(lambda _: lost.set())
        try:
            await connection.add_listener(CHANNEL, self._on_notification)
            self._ready.set()
            log.info("event_listener_started")
            while not lost.is_set():
                try:
                    async with asyncio.timeout(self._keepalive):
                        await lost.wait()
                except TimeoutError:
                    # Detects connections that died without a FIN (network failures).
                    async with asyncio.timeout(self._connect_timeout):
                        await connection.execute("SELECT 1")
            log.warning("event_listener_lost")
        finally:
            if not connection.is_closed():
                await connection.close(timeout=self._connect_timeout)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


router = APIRouter(tags=["events"])


def _sse(event: Event) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _stream(broker: EventBroker, user_id: UUID) -> AsyncIterator[str]:
    # Subscribing inside the generator ties the subscription to the response's lifetime.
    with broker.subscribe(user_id) as subscription:
        # Sent once the subscription is active: from here on no event is missed.
        yield f"retry: {RECONNECT_DELAY_MS}\n\n"
        while True:
            try:
                async with asyncio.timeout(HEARTBEAT_INTERVAL):
                    event = await subscription.queue.get()
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _sse(event)


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-Sent Events stream of `Event` objects for the current user",
            "content": {"text/event-stream": {"schema": Event.model_json_schema()}},
        },
        503: {"description": "The event listener is unavailable"},
    },
)
async def stream_events(
    request: Request, user_id: Annotated[UUID, Depends(get_current_user_id)]
) -> StreamingResponse:
    """Stream the current user's events. Each SSE ``event`` is the event type, ``data``
    the JSON-encoded event."""
    broker: EventBroker = request.app.state.events
    try:
        await broker.wait_ready()
    except TimeoutError:
        raise ProblemError(503, detail="Live updates are temporarily unavailable.") from None
    return StreamingResponse(
        _stream(broker, user_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

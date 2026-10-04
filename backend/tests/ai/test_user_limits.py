"""Per-user limit on parallel LLM requests in the API (#191)."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from app.ai.llm.user_limits import (
    ERROR_CODE,
    RETRY_AFTER_SECONDS,
    UserLLMLimiter,
    optional_user_llm_slot,
    user_llm_slot,
)
from app.auth.dependencies import get_current_session
from app.auth.sessions import CurrentSession
from app.core.errors import ProblemError, install_error_handlers
from app.search.router import get_embedder
from app.users.models import UserRole

ALICE = uuid.UUID("0190a000-0000-7000-8000-00000000000a")
BOB = uuid.UUID("0190a000-0000-7000-8000-00000000000b")


def test_slots_are_counted_per_user() -> None:
    limiter = UserLLMLimiter(2)

    first, second = limiter.acquire(ALICE), limiter.acquire(ALICE)
    assert limiter.try_acquire(ALICE) is None
    # Other users are not affected.
    assert limiter.try_acquire(BOB) is not None

    first.release()
    first.release()  # idempotent
    assert limiter.active(ALICE) == 1
    assert limiter.try_acquire(ALICE) is not None
    second.release()


def test_refusal_is_429_with_retry_after() -> None:
    limiter = UserLLMLimiter(1)
    limiter.acquire(ALICE)

    with pytest.raises(ProblemError) as raised:
        limiter.acquire(ALICE)

    assert raised.value.status == 429
    assert raised.value.headers == {"Retry-After": str(RETRY_AFTER_SECONDS)}
    assert raised.value.extensions["error_code"] == ERROR_CODE


def test_released_users_are_forgotten() -> None:
    limiter = UserLLMLimiter(3)
    limiter.acquire(ALICE).release()

    assert limiter._active == {}


def _app(limiter: UserLLMLimiter, gate: asyncio.Event) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.state.user_llm_limiter = limiter
    app.dependency_overrides[get_current_session] = lambda: CurrentSession(
        session_id=uuid.uuid4(), user_id=ALICE, role=UserRole.USER
    )

    @app.post("/stream", dependencies=[Depends(user_llm_slot)])
    async def stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            yield "first\n"
            await gate.wait()
            yield "last\n"

        return StreamingResponse(body(), media_type="text/plain")

    @app.post("/optional")
    async def optional(slot: Annotated[bool, Depends(optional_user_llm_slot)]) -> bool:
        return slot

    return app


async def test_slot_is_held_until_the_stream_ends() -> None:
    limiter, gate = UserLLMLimiter(1), asyncio.Event()
    transport = ASGITransport(app=_app(limiter, gate))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        running = asyncio.create_task(http.post("/stream"))
        for _ in range(100):
            if limiter.active(ALICE):
                break
            await asyncio.sleep(0.01)
        assert limiter.active(ALICE) == 1

        refused = await http.post("/stream")
        assert refused.status_code == 429
        assert refused.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
        assert refused.json()["error_code"] == ERROR_CODE
        # Optional slots never refuse: the request goes on without the model.
        assert (await http.post("/optional")).json() is False

        gate.set()
        assert (await running).text == "first\nlast\n"
        assert limiter.active(ALICE) == 0
        assert (await http.post("/optional")).json() is True
        assert limiter.active(ALICE) == 0


async def test_slot_is_released_when_the_client_goes_away() -> None:
    limiter, gate = UserLLMLimiter(1), asyncio.Event()
    transport = ASGITransport(app=_app(limiter, gate))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        running = asyncio.create_task(http.post("/stream"))
        for _ in range(100):
            if limiter.active(ALICE):
                break
            await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        for _ in range(100):
            if not limiter.active(ALICE):
                break
            await asyncio.sleep(0.01)

    assert limiter.active(ALICE) == 0


def test_search_without_free_slot_uses_full_text_only() -> None:
    assert get_embedder(None, None, slot=False) is None  # type: ignore[arg-type]

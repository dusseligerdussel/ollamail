"""Limit on parallel LLM requests per user in the API (#191).

One user with many parallel requests ("ask your inbox", reply drafts, search) would keep a
CPU-only model busy for everybody. Each such request takes a slot of its user for as long
as it runs, a streamed answer until the stream ends; without a free slot the request is
refused with 429 and ``Retry-After`` instead of waiting. The global limit of the API
process (``OLLAMAIL_LLM_API_CONCURRENCY``) is enforced by the gateway itself.

Counters live in the process: with several API replicas a user can use the limit once per
replica.
"""

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request

from app.auth.dependencies import CurrentSessionDep
from app.core.errors import ProblemError

# Seconds a refused client should wait before trying again.
RETRY_AFTER_SECONDS = 5
ERROR_CODE = "llm_busy"
# OpenAPI entry for endpoints that take a slot.
LLM_BUSY: dict[int | str, dict[str, Any]] = {
    429: {"description": "Too many parallel AI requests of the user (`error_code` `llm_busy`)"}
}


class UserSlot:
    """A held slot; ``release`` is idempotent."""

    def __init__(self, limiter: "UserLLMLimiter", user_id: UUID) -> None:
        self._limiter = limiter
        self._user_id = user_id
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter._release(self._user_id)


class UserLLMLimiter:
    """At most ``limit`` slots per user at a time; acquiring never waits."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, limit)
        self._active: dict[UUID, int] = {}

    def active(self, user_id: UUID) -> int:
        return self._active.get(user_id, 0)

    def try_acquire(self, user_id: UUID) -> UserSlot | None:
        """A slot of ``user_id``, or ``None`` if all are taken."""
        active = self._active.get(user_id, 0)
        if active >= self.limit:
            return None
        self._active[user_id] = active + 1
        return UserSlot(self, user_id)

    def acquire(self, user_id: UUID) -> UserSlot:
        """A slot of ``user_id``; 429 with ``Retry-After`` if all are taken."""
        slot = self.try_acquire(user_id)
        if slot is None:
            raise ProblemError(
                429,
                detail="Too many AI requests at the same time. Wait for one to finish.",
                error_code=ERROR_CODE,
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        return slot

    def _release(self, user_id: UUID) -> None:
        active = self._active.get(user_id, 0) - 1
        if active > 0:
            self._active[user_id] = active
        else:
            self._active.pop(user_id, None)


def get_user_llm_limiter(request: Request) -> UserLLMLimiter:
    limiter: UserLLMLimiter = request.app.state.user_llm_limiter
    return limiter


LimiterDep = Annotated[UserLLMLimiter, Depends(get_user_llm_limiter)]


async def user_llm_slot(current: CurrentSessionDep, limiter: LimiterDep) -> AsyncIterator[None]:
    """FastAPI dependency: a slot of the current user for the whole request, including a
    streamed response (the dependency ends after the response is sent or aborted)."""
    slot = limiter.acquire(current.user_id)
    try:
        yield
    finally:
        slot.release()


async def optional_user_llm_slot(
    current: CurrentSessionDep, limiter: LimiterDep
) -> AsyncIterator[bool]:
    """Like :func:`user_llm_slot`, but yields ``False`` instead of refusing the request
    (for requests that can do without the model, e.g. search without embeddings)."""
    slot = limiter.try_acquire(current.user_id)
    try:
        yield slot is not None
    finally:
        if slot is not None:
            slot.release()

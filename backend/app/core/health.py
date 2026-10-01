"""Liveness (``/healthz``) and readiness (``/readyz``) probes.

Modules register further readiness checks (queue, LLM, ...) with
``register_readiness_check(app, name, check)``. A check is an async callable that raises
if its dependency is unavailable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.logging import get_logger

ReadinessCheck = Callable[[], Awaitable[None]]

log = get_logger(__name__)
router = APIRouter(tags=["health"])


class ReadinessRegistry:
    def __init__(self, timeout: float = 3.0) -> None:
        self.timeout = timeout
        self._checks: dict[str, ReadinessCheck] = {}

    def register(self, name: str, check: ReadinessCheck) -> None:
        if name in self._checks:
            raise ValueError(f"readiness check {name!r} is already registered")
        self._checks[name] = check

    async def _run_one(self, name: str, check: ReadinessCheck) -> bool:
        try:
            async with asyncio.timeout(self.timeout):
                await check()
        except Exception as exc:
            # Type only: driver messages may contain connection details.
            log.warning("readiness_check_failed", check=name, error_type=type(exc).__name__)
            return False
        return True

    async def run(self) -> dict[str, bool]:
        names = list(self._checks)
        results = await asyncio.gather(*(self._run_one(n, self._checks[n]) for n in names))
        return dict(zip(names, results, strict=True))


def register_readiness_check(app: FastAPI, name: str, check: ReadinessCheck) -> None:
    registry: ReadinessRegistry = app.state.readiness
    registry.register(name, check)


class HealthStatus(BaseModel):
    status: Literal["ok"]


class ReadinessStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    checks: dict[str, Literal["ok", "failed"]]


@router.get("/healthz")
async def healthz() -> HealthStatus:
    """Liveness probe: reports that the process is up."""
    return HealthStatus(status="ok")


@router.get(
    "/readyz",
    response_model=ReadinessStatus,
    responses={503: {"model": ReadinessStatus, "description": "A dependency is unavailable"}},
)
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe: 200 if all registered checks pass, otherwise 503."""
    registry: ReadinessRegistry = request.app.state.readiness
    results = await registry.run()
    ready = all(results.values())
    body = ReadinessStatus(
        status="ok" if ready else "unavailable",
        checks={name: "ok" if ok else "failed" for name, ok in results.items()},
    )
    return JSONResponse(body.model_dump(), status_code=200 if ready else 503)

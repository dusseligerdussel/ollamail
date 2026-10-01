"""Application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.core.config import Settings, get_settings
from app.core.crypto import configure_keyring
from app.core.db import Database
from app.core.errors import install_error_handlers
from app.core.health import ReadinessRegistry, register_readiness_check
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.logging)
    database = Database(settings.database)
    llm = LLMGateway(EnvConfigResolver(settings.llm))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Refuse to start without a valid OLLAMAIL_SECRET_KEY.
        configure_keyring(settings.security)
        pull = None
        if settings.llm.pull_missing_models:
            pull = asyncio.create_task(llm.pull_missing_models())
        yield
        if pull is not None:
            pull.cancel()
            with suppress(asyncio.CancelledError):
                await pull
        await llm.aclose()
        await database.dispose()

    app = FastAPI(title="ollamail", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.readiness = ReadinessRegistry()
    register_readiness_check(app, "database", database.ping)
    app.state.llm = llm
    if settings.llm.readiness_check:
        register_readiness_check(app, "llm", llm.check_ready)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    return app


app = create_app()

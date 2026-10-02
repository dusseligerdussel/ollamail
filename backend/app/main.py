"""Application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.ai.llm import EnvConfigResolver, LLMGateway
from app.audit.router import router as audit_router
from app.auth.csrf import CSRFMiddleware
from app.auth.providers import AuthProviderRegistry
from app.auth.router import router as auth_router
from app.auth.router import setup_router
from app.auth.setup import log_setup_status
from app.core.config import Settings, get_settings
from app.core.crypto import configure_keyring
from app.core.db import Database
from app.core.errors import install_error_handlers
from app.core.events import EventBroker
from app.core.events import router as events_router
from app.core.health import ReadinessRegistry, register_readiness_check
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.openapi import generate_operation_id
from app.todos.router import router as todos_router
from app.users.router import router as users_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.logging)
    database = Database(settings.database)
    llm = LLMGateway(EnvConfigResolver(settings.llm))
    events = EventBroker(settings.database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Refuse to start without a valid OLLAMAIL_SECRET_KEY.
        configure_keyring(settings.security)
        await log_setup_status(database, settings)
        pull = None
        if settings.llm.pull_missing_models:
            pull = asyncio.create_task(llm.pull_missing_models())
        yield
        if pull is not None:
            pull.cancel()
            with suppress(asyncio.CancelledError):
                await pull
        await llm.aclose()
        await events.stop()
        await database.dispose()

    app = FastAPI(
        title="ollamail",
        lifespan=lifespan,
        generate_unique_id_function=generate_operation_id,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.events = events
    app.state.readiness = ReadinessRegistry()
    register_readiness_check(app, "database", database.ping)
    app.state.llm = llm
    if settings.llm.readiness_check:
        register_readiness_check(app, "llm", llm.check_ready)

    app.state.auth_providers = AuthProviderRegistry()

    install_error_handlers(app)
    # Added first, so it runs inside RequestContextMiddleware (403s carry a request ID).
    app.add_middleware(CSRFMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    app.include_router(events_router)
    app.include_router(setup_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(todos_router)
    app.include_router(audit_router)
    return app


app = create_app()

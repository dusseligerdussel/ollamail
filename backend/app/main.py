"""Application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.errors import install_error_handlers
from app.core.health import ReadinessRegistry, register_readiness_check
from app.core.health import router as health_router
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.openapi import generate_operation_id


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.logging)
    database = Database(settings.database)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await database.dispose()

    app = FastAPI(
        title="ollamail",
        lifespan=lifespan,
        generate_unique_id_function=generate_operation_id,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.readiness = ReadinessRegistry()
    register_readiness_check(app, "database", database.ping)

    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    return app


app = create_app()

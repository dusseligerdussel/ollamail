"""Application entry point."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(title="ollamail")

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness probe: reports that the process is up."""
        return {"status": "ok"}

    return app


app = create_app()

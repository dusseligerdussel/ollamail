"""``python -m app.cli search ...``: index status and resizing the vector column.

Output contains counts, model names and dimensions only, never mail content.
"""

import asyncio
from typing import Annotated

import typer

from app.ai.llm import EnvConfigResolver, LLMTask
from app.core.config import get_settings
from app.core.db import Database
from app.search import service

search_cli = typer.Typer(name="search", no_args_is_help=True, help="Hybrid search index.")


async def _current_model() -> str:
    resolver = EnvConfigResolver(get_settings().llm)
    return (await resolver.resolve(LLMTask.EMBEDDINGS)).model


async def _status() -> tuple[service.IndexStatus, str]:
    database = Database(get_settings().database)
    try:
        async with database.sessionmaker() as session:
            return await service.index_status(session), await _current_model()
    finally:
        await database.dispose()


@search_cli.command("status")
def status_command() -> None:
    """Show chunks, embeddings per model, the active and the configured model."""
    status, current = asyncio.run(_status())
    configured_dimensions = get_settings().search.embedding_dimensions
    typer.echo(f"Chunks: {status.chunks}")
    typer.echo(f"Active model: {status.active_model or '-'}")
    typer.echo(f"Configured model: {current}")
    for model, count in sorted(status.embeddings.items()):
        typer.echo(f"Embeddings ({model}): {count}")
    typer.echo(f"Vector dimensions: {status.dimensions} (configured: {configured_dimensions})")
    if status.dimensions != configured_dimensions:
        typer.echo("Dimensions differ: run 'python -m app.cli search resize'.", err=True)


async def _resize(dimensions: int) -> None:
    database = Database(get_settings().database)
    try:
        async with database.sessionmaker() as session:
            await service.resize_embeddings(session, dimensions, await _current_model())
            await session.commit()
    finally:
        await database.dispose()


@search_cli.command("resize")
def resize_command(
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Set the vector column to OLLAMAIL_SEARCH_EMBEDDING_DIMENSIONS (embedding model with
    another vector length). Deletes all vectors; the worker rebuilds them in the
    background, full-text search keeps working meanwhile."""
    dimensions = get_settings().search.embedding_dimensions
    if not yes:
        typer.confirm(
            f"Delete all embeddings and change the vector column to {dimensions} dimensions?",
            abort=True,
        )
    asyncio.run(_resize(dimensions))
    typer.echo(f"Vector column resized to {dimensions} dimensions; embeddings are rebuilt.")

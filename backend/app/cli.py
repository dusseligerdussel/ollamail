"""Command line interface: ``python -m app.cli <command>``.

Modules add their commands to ``cli`` with ``@cli.command()``. In the containers::

    docker compose -f deploy/compose.yaml run --rm api python -m app.cli --help
"""

import asyncio

import typer

from app.core.config import get_settings
from app.core.crypto import CryptoError, RotationResult, configure_keyring, generate_key
from app.core.crypto import rotate_keys as rotate_all
from app.core.db import Database
from app.core.logging import configure_logging

cli = typer.Typer(name="ollamail", no_args_is_help=True, add_completion=False)


@cli.callback()
def main() -> None:
    """ollamail administration commands."""
    configure_logging(get_settings().logging)


@cli.command("generate-key")
def generate_key_command() -> None:
    """Print a new random master key for OLLAMAIL_SECRET_KEY."""
    typer.echo(generate_key())


async def _rotate() -> RotationResult:
    # Imported here so every models module is registered on Base.metadata.
    from app.models import Base

    settings = get_settings()
    keyring = configure_keyring(settings.security)
    database = Database(settings.database)
    try:
        async with database.engine.begin() as connection:
            return await rotate_all(connection, keyring, Base.metadata)
    finally:
        await database.dispose()


@cli.command("rotate-keys")
def rotate_keys_command() -> None:
    """Re-encrypt all stored secrets with the current OLLAMAIL_SECRET_KEY.

    Set the new key as OLLAMAIL_SECRET_KEY and the previous one(s) in
    OLLAMAIL_SECRET_KEYS_OLD, run this command, then remove the old keys.
    """
    try:
        result = asyncio.run(_rotate())
    except CryptoError as exc:
        typer.echo(f"Key rotation failed, nothing was changed: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"Checked {result.checked} values in {result.tables} tables, re-encrypted {result.rotated}."
    )


if __name__ == "__main__":
    cli()

"""Command line interface: ``python -m app.cli <command>``.

Modules add their commands to ``cli`` with ``@cli.command()``. In the containers::

    docker compose -f deploy/compose.yaml run --rm api python -m app.cli --help
"""

import asyncio
from typing import Annotated

import typer
from pydantic import ValidationError

from app import audit
from app.core.config import get_settings
from app.core.crypto import CryptoError, RotationResult, configure_keyring, generate_key
from app.core.crypto import rotate_keys as rotate_all
from app.core.db import Database
from app.core.logging import configure_logging
from app.processing.cli import processing_cli
from app.search.cli import search_cli

cli = typer.Typer(name="ollamail", no_args_is_help=True, add_completion=False)
cli.add_typer(processing_cli)
cli.add_typer(search_cli)


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
            result = await rotate_all(connection, keyring, Base.metadata)
            details = {
                "tables": result.tables,
                "checked": result.checked,
                "rotated": result.rotated,
            }
            await audit.record(
                connection, audit.SYSTEM, audit.AuditAction.KEYS_ROTATED, None, details
            )
            return result
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


@cli.command("setup-token")
def setup_token_command() -> None:
    """Print the token for creating the first admin (POST /api/setup)."""
    from app.auth.setup import setup_token

    settings = get_settings()
    try:
        configure_keyring(settings.security)
    except CryptoError as exc:
        typer.echo(f"Cannot derive the setup token: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(setup_token(settings))


async def _create_admin(email: str, display_name: str, password: str) -> None:
    from app.auth.passwords import hash_password
    from app.users.models import UserRole
    from app.users.service import add_local_user

    settings = get_settings()
    database = Database(settings.database)
    try:
        async with database.sessionmaker() as db:
            user = await add_local_user(
                db,
                email=email,
                display_name=display_name,
                password_hash=await hash_password(password),
                role=UserRole.ADMIN,
            )
            target = audit.Target.of(audit.TargetType.USER, user.id)
            details = {"role": user.role, "via": "cli"}
            await audit.record(db, audit.SYSTEM, audit.AuditAction.USER_CREATED, target, details)
            await db.commit()
    finally:
        await database.dispose()


@cli.command("create-admin")
def create_admin_command(
    email: Annotated[str, typer.Option(prompt=True, help="E-mail address (login name).")],
    display_name: Annotated[str, typer.Option(prompt=True, help="Display name.")],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    """Create a local admin account, also when users exist (emergency access).

    Prefer the setup wizard for the first admin. The password is read from a prompt;
    passing it as option puts it into the shell history.
    """
    from app.core.errors import ProblemError
    from app.users.schemas import UserCreate
    from app.users.service import check_password_policy

    try:
        data = UserCreate(email=email, display_name=display_name, password=password)
        check_password_policy(get_settings().auth, data.password)
        asyncio.run(_create_admin(data.email, data.display_name, data.password))
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors())
        typer.echo(f"Invalid input: {fields}", err=True)
        raise typer.Exit(code=1) from None
    except ProblemError as exc:
        typer.echo(f"Could not create the admin: {exc.detail}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Admin created.")


if __name__ == "__main__":
    cli()

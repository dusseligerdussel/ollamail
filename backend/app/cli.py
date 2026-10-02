"""Command line interface: ``python -m app.cli <command>``.

Modules add their commands to ``cli`` with ``@cli.command()``. In the containers::

    docker compose -f deploy/compose.yaml run --rm api python -m app.cli --help
"""

import asyncio
from typing import TYPE_CHECKING, Annotated

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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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


async def _enable_local_login(db: "AsyncSession") -> bool:
    """Switch local login back on (emergency access); ``True`` if it was off."""
    from app.auth.policy import get_policy_for_update

    policy = await get_policy_for_update(db)
    if policy.local_login_enabled:
        return False
    policy.local_login_enabled = True
    await audit.record(
        db,
        audit.SYSTEM,
        audit.AuditAction.IDP_CONFIG_CHANGED,
        audit.Target.of(audit.TargetType.SETTINGS, "auth"),
        {"kind": "local", "change": "enabled", "via": "cli"},
    )
    return True


async def _create_admin(email: str, display_name: str, password: str) -> bool:
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
            enabled = await _enable_local_login(db)
            await db.commit()
            return enabled
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
    passing it as option puts it into the shell history. Switches local login back on if
    an admin had disabled it.
    """
    from app.core.errors import ProblemError
    from app.users.schemas import UserCreate
    from app.users.service import check_password_policy

    try:
        data = UserCreate(email=email, display_name=display_name, password=password)
        check_password_policy(get_settings().auth, data.password)
        enabled = asyncio.run(_create_admin(data.email, data.display_name, data.password))
    except ValidationError as exc:
        fields = ", ".join(str(error["loc"][0]) for error in exc.errors())
        typer.echo(f"Invalid input: {fields}", err=True)
        raise typer.Exit(code=1) from None
    except ProblemError as exc:
        typer.echo(f"Could not create the admin: {exc.detail}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Admin created.")
    if enabled:
        typer.echo(_LOCAL_LOGIN_ENABLED)


_LOCAL_LOGIN_ENABLED = "Local login was disabled and has been switched on again."


class _ResetError(Exception):
    pass


async def _reset_password(email: str, password: str, *, activate: bool) -> bool:
    from sqlalchemy import delete, select

    from app.auth.models import LOCAL_PROVIDER, Identity, Invitation
    from app.auth.passwords import hash_password
    from app.auth.rate_limit import reset as reset_counter
    from app.auth.service import account_key
    from app.auth.sessions import revoke_user_sessions
    from app.users.service import get_user_by_email

    settings = get_settings()
    configure_keyring(settings.security)
    database = Database(settings.database)
    try:
        async with database.sessionmaker() as db:
            user = await get_user_by_email(db, email)
            if user is None:
                raise _ResetError("There is no user with this e-mail address.")
            if not user.is_active and not activate:
                raise _ResetError("The user is deactivated; add --activate to reactivate them.")
            password_hash = await hash_password(password)
            identity = await db.scalar(
                select(Identity).where(
                    Identity.user_id == user.id, Identity.provider == LOCAL_PROVIDER
                )
            )
            if identity is None:
                db.add(
                    Identity(
                        user_id=user.id,
                        provider=LOCAL_PROVIDER,
                        subject=str(user.id),
                        password_hash=password_hash,
                    )
                )
            else:
                identity.password_hash = password_hash
            target = audit.Target.of(audit.TargetType.USER, user.id)
            if not user.is_active:
                user.is_active = True
                await audit.record(
                    db, audit.SYSTEM, audit.AuditAction.USER_REACTIVATED, target, {"via": "cli"}
                )
            # Old sessions end, a pending invitation and the lockout counter are cleared.
            sessions = await revoke_user_sessions(db, user.id)
            await db.execute(delete(Invitation).where(Invitation.user_id == user.id))
            await reset_counter(db, account_key(settings, email))
            await audit.record(
                db,
                audit.SYSTEM,
                audit.AuditAction.USER_PASSWORD_SET,
                target,
                {"via": "cli", "sessions": sessions},
            )
            enabled = await _enable_local_login(db)
            await db.commit()
            return enabled
    finally:
        await database.dispose()


@cli.command("reset-password")
def reset_password_command(
    email: Annotated[str, typer.Option(prompt=True, help="E-mail address of the user.")],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
    activate: Annotated[
        bool, typer.Option("--activate", help="Reactivate the user if deactivated.")
    ] = False,
) -> None:
    """Set a new local password (emergency access), also for users who sign in via SSO.

    Ends the user's sessions, clears the login lockout and switches local login back on
    if it was disabled. The role is not changed; use create-admin for a new admin.
    """
    from app.core.errors import ProblemError
    from app.users.schemas import normalize_email
    from app.users.service import check_password_policy

    try:
        normalized = normalize_email(email)
        check_password_policy(get_settings().auth, password)
        enabled = asyncio.run(_reset_password(normalized, password, activate=activate))
    except ValueError:
        typer.echo("Invalid input: email", err=True)
        raise typer.Exit(code=1) from None
    except ProblemError as exc:
        typer.echo(f"Could not reset the password: {exc.detail}", err=True)
        raise typer.Exit(code=1) from None
    except (_ResetError, CryptoError) as exc:
        typer.echo(f"Could not reset the password: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Password set; the user's sessions have ended.")
    if enabled:
        typer.echo(_LOCAL_LOGIN_ENABLED)


if __name__ == "__main__":
    cli()

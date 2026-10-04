"""``python -m app.cli notifications ...``: setting up Web Push."""

import typer

from app.notifications.vapid import generate_key_pair

notifications_cli = typer.Typer(
    name="notifications", no_args_is_help=True, help="Notifications and Web Push."
)


@notifications_cli.command("vapid-keys")
def vapid_keys_command() -> None:
    """Print a new VAPID key pair for Web Push as environment variables.

    Keep the private key secret. Changing the keys later makes every registered device
    invalid: users switch Web Push on again on each device.
    """
    public, private = generate_key_pair()
    typer.echo(f"OLLAMAIL_NOTIFICATIONS_VAPID_PUBLIC_KEY={public}")
    typer.echo(f"OLLAMAIL_NOTIFICATIONS_VAPID_PRIVATE_KEY={private}")

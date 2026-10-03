"""``python -m app.cli processing ...``: reprocess messages, switch processing per mailbox.

Output contains counts and IDs only, never mail content (admins do not read mail).
"""

import asyncio
import importlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

import typer

from app.core.config import get_settings
from app.core.db import Database
from app.mail.models import Mailbox
from app.processing import service
from app.processing.steps import registry
from app.processing.tasks import Priority, requeue_messages
from app.worker import TASK_MODULES, app

processing_cli = typer.Typer(
    name="processing", no_args_is_help=True, help="Mail processing pipeline."
)


def _load_steps() -> None:
    # Steps register when their feature module is imported.
    for module in TASK_MODULES:
        importlib.import_module(module)


def _utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


async def _reprocess(
    mailbox_id: uuid.UUID | None,
    since: datetime | None,
    until: datetime | None,
    steps: Sequence[str] | None,
) -> int:
    database = Database(get_settings().database)
    try:
        async with database.sessionmaker() as session:
            message_ids = await service.select_messages(
                session, mailbox_id=mailbox_id, since=_utc(since), until=_utc(until)
            )
            await service.reset_steps(session, message_ids, steps)
            await session.commit()
    finally:
        await database.dispose()
    async with app.open_async():
        await requeue_messages(message_ids, Priority.REPROCESS)
    return len(message_ids)


@processing_cli.command("reprocess")
def reprocess_command(
    mailbox: Annotated[uuid.UUID | None, typer.Option(help="Only this mailbox (ID).")] = None,
    since: Annotated[
        datetime | None, typer.Option(help="Only mails received at or after this time (UTC).")
    ] = None,
    until: Annotated[
        datetime | None, typer.Option(help="Only mails received before this time (UTC).")
    ] = None,
    step: Annotated[
        list[str] | None, typer.Option(help="Only this step; repeat for several.")
    ] = None,
) -> None:
    """Process messages again, even if they are done or failed.

    The jobs run behind new mail and the initial import. Mailboxes with processing
    disabled are left out.
    """
    _load_steps()
    known = {s.name for s in registry.ordered()}
    unknown = sorted(set(step or ()) - known)
    if unknown:
        typer.echo(
            f"Unknown step(s): {', '.join(unknown)}. Known: {', '.join(sorted(known)) or '-'}",
            err=True,
        )
        raise typer.Exit(code=1)
    count = asyncio.run(_reprocess(mailbox, since, until, step or None))
    typer.echo(f"Queued {count} messages for reprocessing.")


async def _set_enabled(mailbox_id: uuid.UUID, enabled: bool) -> bool:
    database = Database(get_settings().database)
    try:
        async with database.sessionmaker() as session:
            if await session.get(Mailbox, mailbox_id) is None:
                return False
            await service.set_mailbox_enabled(session, mailbox_id, enabled)
            await session.commit()
    finally:
        await database.dispose()
    return True


def _switch(mailbox_id: uuid.UUID, enabled: bool) -> None:
    if not asyncio.run(_set_enabled(mailbox_id, enabled)):
        typer.echo(f"Mailbox {mailbox_id} not found.", err=True)
        raise typer.Exit(code=1)
    state = "enabled" if enabled else "disabled"
    typer.echo(f"Processing {state} for mailbox {mailbox_id}.")


@processing_cli.command("disable")
def disable_command(mailbox_id: uuid.UUID) -> None:
    """Stop processing new and queued mails of a mailbox (e.g. no AI for it).

    Existing results are kept.
    """
    _switch(mailbox_id, enabled=False)


@processing_cli.command("enable")
def enable_command(mailbox_id: uuid.UUID) -> None:
    """Process the mails of a mailbox again; missed mails are picked up automatically."""
    _switch(mailbox_id, enabled=True)


async def _include_older(mailbox_id: uuid.UUID) -> int | None:
    database = Database(get_settings().database)
    try:
        async with database.sessionmaker() as session:
            if await session.get(Mailbox, mailbox_id) is None:
                return None
            message_ids = await service.include_older(session, mailbox_id)
            await session.commit()
    finally:
        await database.dispose()
    async with app.open_async():
        await requeue_messages(message_ids, Priority.REPROCESS)
    return len(message_ids)


@processing_cli.command("include-older")
def include_older_command(mailbox_id: uuid.UUID) -> None:
    """Classify the older mails of a mailbox too (triage, todos).

    By default only mails of the last OLLAMAIL_PROCESSING_BACKFILL_LLM_DAYS days are
    classified. This processes the skipped older mails, behind new mail, and keeps doing
    so for this mailbox from now on.
    """
    _load_steps()
    count = asyncio.run(_include_older(mailbox_id))
    if count is None:
        typer.echo(f"Mailbox {mailbox_id} not found.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Queued {count} older messages of mailbox {mailbox_id}.")

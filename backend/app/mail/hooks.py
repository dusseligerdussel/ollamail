"""Extension points of the mail module for other features.

``message_stored`` is called once for every *new* message, after the transaction that
stored it has been committed (so a job deferred from a handler sees the row). Messages
that were already stored (flag updates, re-fetches) do not trigger it. ``backfill`` is true
for messages of the initial import, so handlers can give newly arrived mail priority.

The processing pipeline hooks in like this (``app/processing/tasks.py``)::

    from app.mail.hooks import MessageStored, on_message_stored

    @on_message_stored
    async def process_stored_message(event: MessageStored) -> None:
        priority = Priority.BACKFILL if event.backfill else Priority.NEW
        await enqueue_processing(event.message_id, priority=priority)

Handlers run in the worker process that syncs the mailbox, so the module registering
them must be imported there (``TASK_MODULES`` in ``app/worker.py``). Handlers should be
quick (defer a job, publish an event) and idempotent. A failing handler is logged and does
not stop the sync or the other handlers; the message stays stored.

``mailbox_deleted`` is called after the background removal (``app.mail.deletion``) has
deleted a mailbox row, with the former owner (``None`` for a shared mailbox). The deletion
of a user waits for it (``app.privacy.tasks``, #177). Same rules: registered in a module of
``TASK_MODULES``, quick, idempotent; a failing handler is logged.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MessageStored:
    mailbox_id: uuid.UUID
    message_id: uuid.UUID
    # Stored by the initial import of a folder, not newly arrived.
    backfill: bool = False


MessageStoredHandler = Callable[[MessageStored], Awaitable[None]]

_handlers: list[MessageStoredHandler] = []


def on_message_stored(handler: MessageStoredHandler) -> MessageStoredHandler:
    """Register ``handler`` (usable as decorator). Registering twice has no effect."""
    if handler not in _handlers:
        _handlers.append(handler)
    return handler


def remove_message_stored_handler(handler: MessageStoredHandler) -> None:
    if handler in _handlers:
        _handlers.remove(handler)


async def message_stored(
    mailbox_id: uuid.UUID, message_id: uuid.UUID, *, backfill: bool = False
) -> None:
    """Notify all handlers that ``message_id`` was stored and committed."""
    event = MessageStored(mailbox_id=mailbox_id, message_id=message_id, backfill=backfill)
    for handler in list(_handlers):
        try:
            await handler(event)
        except Exception as exc:
            log.error(
                "mail_message_stored_handler_failed",
                mailbox_id=str(mailbox_id),
                message_id=str(message_id),
                error_type=type(exc).__name__,
            )


@dataclass(frozen=True, slots=True)
class MailboxDeleted:
    mailbox_id: uuid.UUID
    owner_user_id: uuid.UUID | None


MailboxDeletedHandler = Callable[[MailboxDeleted], Awaitable[None]]

_deleted_handlers: list[MailboxDeletedHandler] = []


def on_mailbox_deleted(handler: MailboxDeletedHandler) -> MailboxDeletedHandler:
    """Register ``handler`` (usable as decorator). Registering twice has no effect."""
    if handler not in _deleted_handlers:
        _deleted_handlers.append(handler)
    return handler


async def mailbox_deleted(mailbox_id: uuid.UUID, owner_user_id: uuid.UUID | None) -> None:
    """Notify all handlers that the row of ``mailbox_id`` was deleted and committed."""
    event = MailboxDeleted(mailbox_id=mailbox_id, owner_user_id=owner_user_id)
    for handler in list(_deleted_handlers):
        try:
            await handler(event)
        except Exception as exc:
            log.error(
                "mail_mailbox_deleted_handler_failed",
                mailbox_id=str(mailbox_id),
                error_type=type(exc).__name__,
            )

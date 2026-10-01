"""Extension points of the mail module for other features.

``message_stored`` is called once for every *new* message, after the transaction that
stored it has been committed (so a job deferred from a handler sees the row). Messages
that were already stored (flag updates, re-fetches) do not trigger it.

The processing pipeline (#19) hooks in like this::

    from app.mail.hooks import MessageStored, on_message_stored

    @on_message_stored
    async def enqueue_processing(event: MessageStored) -> None:
        await process_message.defer_async(message_id=str(event.message_id))

Handlers run in the worker process that syncs the mailbox, so the module registering
them must be imported there (``TASK_MODULES`` in ``app/worker.py``). Handlers should be
quick (defer a job, publish an event) and idempotent. A failing handler is logged and does
not stop the sync or the other handlers; the message stays stored.
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


async def message_stored(mailbox_id: uuid.UUID, message_id: uuid.UUID) -> None:
    """Notify all handlers that ``message_id`` was stored and committed."""
    event = MessageStored(mailbox_id=mailbox_id, message_id=message_id)
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

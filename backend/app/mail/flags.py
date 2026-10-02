"""Write flag changes made in the app (read/unread) back to the mail server.

The stored flags are the source: the job writes whatever is stored when it runs, so
repeated or reordered jobs end in the latest state. Failures that will not go away by
retrying (missing message, read-only access, bad credentials) are logged as codes and
dropped; the next sync brings the server state back if the write never happens.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.mail.models import Mailbox, Message
from app.mail.providers.base import ConnectionFailedError, MessageNotFoundError, ProviderError
from app.mail.providers.registry import ProviderFactory, registry
from app.mail.sync.engine import mailbox_config

log = get_logger(__name__)


async def write_flags(
    session: AsyncSession,
    message_id: uuid.UUID,
    *,
    provider_factory: ProviderFactory = registry.create,
) -> bool:
    """Set the server flags of a message to the stored ones; ``True`` if written."""
    message = await session.get(Message, message_id)
    if message is None:
        return False
    mailbox = await session.get(Mailbox, message.mailbox_id)
    if mailbox is None:
        return False
    try:
        provider = provider_factory(mailbox_config(mailbox))
        try:
            await provider.set_flags(message.remote_ref, frozenset(message.flags or []))
        finally:
            await provider.aclose()
    except ConnectionFailedError:
        # Temporary: the job is retried.
        raise
    except MessageNotFoundError:
        log.info("mail_flags_message_gone", message_id=str(message_id))
        return False
    except ProviderError as exc:
        log.warning("mail_flags_write_failed", message_id=str(message_id), error=exc.code)
        return False
    log.info("mail_flags_written", message_id=str(message_id))
    return True

"""Write the category back to the mail server (opt-in per mailbox).

Modes (``TriageMailboxSettings.write_back``):

* ``label``: ``MailProvider.apply_label`` with ``<prefix><key>``, e.g. ``ollamail/newsletter``.
  The provider decides what that is: IMAP keyword (or a copy into a folder if the server
  has no keywords), Gmail label, Graph category. The previous label is removed first.
* ``move``: ``MailProvider.move`` into an existing folder with that name; without such a
  folder nothing happens.

A result whose category changed carries ``write_back_pending``. The ``triage_write_back``
pipeline step handles new mails right away; the periodic job ``triage.write_back`` picks up
corrections and mailboxes where write-back was just enabled. Both are idempotent.
"""

import uuid
from collections.abc import Callable, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.mail.models import Folder, Mailbox, Message
from app.mail.providers.base import MailboxConfig, MailProvider, MessageNotFoundError
from app.mail.providers.registry import registry as provider_registry
from app.triage.categories import slugify
from app.triage.models import (
    TriageCategory,
    TriageMailboxSettings,
    TriageResult,
    WriteBackMode,
)

log = get_logger(__name__)

ProviderFactory = Callable[[MailboxConfig], MailProvider]


def label_for(category: TriageCategory, prefix: str) -> str:
    """Stable server-side name of a category: ``<prefix><builtin key or slug of name>``."""
    return f"{prefix}{category.builtin_key or slugify(category.name)}"


def mailbox_config(mailbox: Mailbox) -> MailboxConfig:
    return MailboxConfig(
        mailbox_id=mailbox.id,
        type=mailbox.type,
        address=mailbox.address,
        settings=dict(mailbox.provider_settings or {}),
        credentials=dict(mailbox.credentials or {}),
    )


async def write_back_mode(session: AsyncSession, mailbox_id: uuid.UUID) -> WriteBackMode:
    mode = await session.scalar(
        select(TriageMailboxSettings.write_back).where(
            TriageMailboxSettings.mailbox_id == mailbox_id
        )
    )
    return mode or WriteBackMode.OFF


async def set_write_back_mode(
    session: AsyncSession, mailbox_id: uuid.UUID, mode: WriteBackMode
) -> None:
    """Switch the mode; enabling it marks all triaged messages of the mailbox for
    write-back."""
    settings = await session.scalar(
        select(TriageMailboxSettings).where(TriageMailboxSettings.mailbox_id == mailbox_id)
    )
    previous = settings.write_back if settings is not None else WriteBackMode.OFF
    if settings is None:
        settings = TriageMailboxSettings(mailbox_id=mailbox_id)
        session.add(settings)
    settings.write_back = mode
    if mode != WriteBackMode.OFF and mode != previous:
        in_mailbox = select(Message.id).where(Message.mailbox_id == mailbox_id)
        await session.execute(
            update(TriageResult)
            .where(TriageResult.message_id.in_(in_mailbox))
            .values(write_back_pending=True)
        )
    await session.flush()


async def _apply(
    session: AsyncSession,
    provider: MailProvider,
    mode: WriteBackMode,
    message: Message,
    result: TriageResult,
    prefix: str,
) -> None:
    desired = label_for(result.category, prefix) if result.category is not None else None
    if desired != result.remote_label:
        if mode == WriteBackMode.LABEL:
            if result.remote_label is not None:
                await provider.remove_label(message.remote_ref, result.remote_label)
            if desired is not None:
                await provider.apply_label(message.remote_ref, desired)
            result.remote_label = desired
        elif mode == WriteBackMode.MOVE and desired is not None:
            folder = await session.scalar(
                select(Folder).where(
                    Folder.mailbox_id == message.mailbox_id,
                    (Folder.name == desired) | (Folder.remote_id == desired),
                )
            )
            if folder is None:
                log.info("triage_write_back_folder_missing", message_id=str(message.id))
            else:
                message.remote_ref = await provider.move(message.remote_ref, folder.remote_id)
                result.remote_label = desired
    result.write_back_pending = False


async def _pending(
    session: AsyncSession, message_ids: Sequence[uuid.UUID]
) -> list[tuple[Message, TriageResult]]:
    rows = await session.execute(
        select(Message, TriageResult)
        .join(TriageResult, TriageResult.message_id == Message.id)
        .where(Message.id.in_(message_ids), TriageResult.write_back_pending.is_(True))
        .with_for_update(of=TriageResult)
    )
    pairs = [(message, result) for message, result in rows]
    for _, result in pairs:
        await session.refresh(result, ["category"])
    return pairs


async def write_back_messages(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    message_ids: Sequence[uuid.UUID],
    *,
    prefix: str,
    provider_factory: ProviderFactory | None = None,
) -> int:
    """Write back the pending results of ``message_ids`` (all in ``mailbox_id``).

    Returns the number of handled messages. Provider errors other than a vanished message
    propagate (the caller retries); writes go through ``session`` without commit.
    """
    mode = await write_back_mode(session, mailbox_id)
    if mode == WriteBackMode.OFF:
        return 0
    pairs = await _pending(session, message_ids)
    if not pairs:
        return 0
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return 0
    factory = provider_factory or provider_registry.create
    provider = factory(mailbox_config(mailbox))
    try:
        for message, result in pairs:
            try:
                await _apply(session, provider, mode, message, result, prefix)
            except MessageNotFoundError:
                # Deleted or moved on the server; the next sync catches up.
                result.write_back_pending = False
    finally:
        await provider.aclose()
    await session.flush()
    return len(pairs)


async def pending_by_mailbox(session: AsyncSession, limit: int) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Pending results in mailboxes with write-back enabled, oldest change first."""
    rows = await session.execute(
        select(Message.mailbox_id, Message.id)
        .join(TriageResult, TriageResult.message_id == Message.id)
        .join(TriageMailboxSettings, TriageMailboxSettings.mailbox_id == Message.mailbox_id)
        .where(
            TriageResult.write_back_pending.is_(True),
            TriageMailboxSettings.write_back != WriteBackMode.OFF,
        )
        .order_by(TriageResult.updated_at)
        .limit(limit)
    )
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    for mailbox_id, message_id in rows:
        grouped.setdefault(mailbox_id, []).append(message_id)
    return grouped

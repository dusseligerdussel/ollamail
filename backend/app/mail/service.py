"""Storing and deleting mail data, keeping database rows and attachment files in step.

Deletion is a hard delete (docs/PRIVACY.md, Art. 17): rows go via ``ON DELETE CASCADE``,
files are removed right after the commit. If the process dies in between, only
unreferenced files remain (no mail data stays reachable); a periodic cleanup can remove
them.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import audit
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.mime import Address, normalize_message
from app.mail.models import Attachment, Folder, Mailbox, Message
from app.mail.providers.base import RawMessage
from app.mail.storage import AttachmentStorage
from app.mail.threads import assign_thread

log = get_logger(__name__)


def _addresses(addresses: Sequence[Address]) -> list[dict[str, object]]:
    return [address.to_json() for address in addresses]


async def store_message(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    raw: RawMessage,
    storage: AttachmentStorage,
    folders: Mapping[str, Folder],
) -> Message:
    """Insert a fetched message (or update flags/folders if it is already stored).

    ``folders`` maps remote folder IDs of the mailbox to their ``Folder`` rows. The caller
    commits; attachment files written here are removed again if this function fails.
    """
    existing = await session.scalar(
        select(Message)
        .where(Message.mailbox_id == mailbox_id, Message.remote_ref == raw.remote_ref)
        .options(selectinload(Message.folders))
    )
    message_folders = [folders[f] for f in raw.folder_ids if f in folders]
    if existing is not None:
        existing.flags = sorted(raw.flags)
        existing.folders = message_folders
        await session.flush()
        return existing

    normalized = normalize_message(raw.raw)
    parsed = normalized.parsed
    timestamp = raw.received_at or parsed.sent_at
    thread = await assign_thread(
        session,
        mailbox_id,
        parsed,
        provider_thread_id=raw.provider_thread_id,
        timestamp=timestamp,
    )
    message = Message(
        id=uuid7(),
        mailbox_id=mailbox_id,
        thread_id=thread.id,
        remote_ref=raw.remote_ref,
        message_id_header=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references=list(parsed.references),
        subject=parsed.subject,
        sender=parsed.sender.to_json() if parsed.sender else None,
        to=_addresses(parsed.to),
        cc=_addresses(parsed.cc),
        bcc=_addresses(parsed.bcc),
        reply_to=_addresses(parsed.reply_to),
        headers=[[name, value] for name, value in parsed.headers],
        sent_at=parsed.sent_at,
        received_at=raw.received_at,
        body_text=normalized.body_text,
        body_html=parsed.html,
        body_main=normalized.body_main,
        signature=normalized.signature,
        language=normalized.language,
        size=parsed.size,
        flags=sorted(raw.flags),
        has_attachments=any(not a.is_inline for a in parsed.attachments),
        folders=message_folders,
    )
    session.add(message)

    written: list[str] = []
    try:
        for parsed_attachment in parsed.attachments:
            attachment_id = uuid7()
            data = parsed_attachment.data
            path = await asyncio.to_thread(storage.write, mailbox_id, attachment_id, data)
            written.append(path)
            message.attachments.append(
                Attachment(
                    id=attachment_id,
                    mailbox_id=mailbox_id,
                    filename=parsed_attachment.filename,
                    content_type=parsed_attachment.content_type,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    content_id=parsed_attachment.content_id,
                    is_inline=parsed_attachment.is_inline,
                    storage_path=path,
                )
            )
        await session.flush()
    except BaseException:
        for path in written:
            await asyncio.to_thread(storage.delete, path)
        raise
    log.info("mail_message_stored", mailbox_id=str(mailbox_id), message_id=str(message.id))
    return message


async def delete_messages(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    remote_refs: Sequence[str],
    storage: AttachmentStorage,
) -> int:
    """Hard-delete messages (e.g. deleted on the server), commit, then remove files."""
    if not remote_refs:
        return 0
    selection = Message.mailbox_id == mailbox_id, Message.remote_ref.in_(remote_refs)
    paths = list(
        await session.scalars(select(Attachment.storage_path).join(Message).where(*selection))
    )
    result = await session.execute(delete(Message).where(*selection).returning(Message.id))
    count = len(result.all())
    await session.commit()
    for path in paths:
        await asyncio.to_thread(storage.delete, path)
    log.info("mail_messages_deleted", mailbox_id=str(mailbox_id), count=count)
    return count


async def delete_mailbox(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    storage: AttachmentStorage,
    actor: audit.Actor = audit.SYSTEM,
) -> bool:
    """Hard-delete a mailbox with all folders, threads, messages, attachments and sync
    state, commit, then remove its attachment files. ``actor`` goes into the audit log."""
    result = await session.execute(
        delete(Mailbox).where(Mailbox.id == mailbox_id).returning(Mailbox.id)
    )
    deleted = result.first() is not None
    if deleted:
        target = audit.Target.of(audit.TargetType.MAILBOX, mailbox_id)
        await audit.record(session, actor, audit.AuditAction.MAILBOX_DELETED, target)
    await session.commit()
    # Also runs if the row was already gone, to clean up leftovers.
    await asyncio.to_thread(storage.delete_mailbox, mailbox_id)
    log.info("mail_mailbox_deleted", mailbox_id=str(mailbox_id), deleted=deleted)
    return deleted

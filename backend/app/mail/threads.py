"""Conversation threading.

Order of evidence, per mailbox (threads never span mailboxes, so access control stays
per mailbox):

1. Server-side conversation ID (Gmail ``threadId``, Graph ``conversationId``).
2. ``In-Reply-To`` / ``References`` pointing at a stored message (and the reverse: stored
   messages that reference this one, if a reply arrived before its parent).
3. Fallback on the normalised subject, only for replies/forwards ("Re:", "AW:", ...) or
   messages with references, and only within ``SUBJECT_FALLBACK_WINDOW``.
"""

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.mime import ParsedMessage
from app.mail.models import Message, Thread

SUBJECT_FALLBACK_WINDOW = timedelta(days=30)

# Reply/forward prefixes in common languages, optionally numbered ("Re[2]:", "AW(3):").
_PREFIX = re.compile(
    r"^\s*(re|aw|antw|antwort|wg|fw|fwd|weitergeleitet|sv|vs|tr|rif|r|odp|ynt|res|enc|rv)"
    r"\s*(\[\d+\]|\(\d+\))?\s*:\s*",
    re.IGNORECASE,
)
# Mailing-list tags like "[project-dev]" in front of the subject.
_LIST_TAG = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")


def strip_subject(subject: str) -> tuple[str, bool]:
    """Return the subject without reply/forward prefixes and list tags, and whether any
    reply/forward prefix was present."""
    value = subject
    was_reply = False
    while True:
        stripped = _LIST_TAG.sub("", value, count=1)
        match = _PREFIX.match(stripped)
        if match:
            was_reply = True
            stripped = stripped[match.end() :]
        if stripped == value:
            return value.strip(), was_reply
        value = stripped


def subject_key(subject: str) -> str:
    stripped, _ = strip_subject(subject)
    return " ".join(stripped.casefold().split())


async def _thread_by_message_ids(
    session: AsyncSession, mailbox_id: uuid.UUID, ids: list[str]
) -> uuid.UUID | None:
    if not ids:
        return None
    rows = await session.execute(
        select(Message.message_id_header, Message.thread_id).where(
            Message.mailbox_id == mailbox_id,
            Message.message_id_header.in_(ids),
            Message.thread_id.is_not(None),
        )
    )
    found = {message_id: thread_id for message_id, thread_id in rows}
    # In-Reply-To is listed first and is the strongest hint; then References, newest first.
    for message_id in ids:
        if message_id in found:
            return found[message_id]
    return None


async def _thread_of_children(
    session: AsyncSession, mailbox_id: uuid.UUID, message_id: str | None
) -> uuid.UUID | None:
    if not message_id:
        return None
    return await session.scalar(
        select(Message.thread_id)
        .where(
            Message.mailbox_id == mailbox_id,
            Message.thread_id.is_not(None),
            (Message.in_reply_to == message_id) | Message.references.contains([message_id]),
        )
        .limit(1)
    )


async def assign_thread(
    session: AsyncSession,
    mailbox_id: uuid.UUID,
    parsed: ParsedMessage,
    *,
    provider_thread_id: str | None = None,
    timestamp: datetime | None = None,
) -> Thread:
    """Find or create the thread for a new message and update its bookkeeping."""
    key = subject_key(parsed.subject)
    thread: Thread | None = None

    if provider_thread_id:
        thread = await session.scalar(
            select(Thread).where(
                Thread.mailbox_id == mailbox_id,
                Thread.provider_thread_id == provider_thread_id,
            )
        )

    if thread is None:
        ids = list(
            dict.fromkeys([*filter(None, [parsed.in_reply_to]), *reversed(parsed.references)])
        )
        thread_id = await _thread_by_message_ids(session, mailbox_id, ids)
        if thread_id is None:
            thread_id = await _thread_of_children(session, mailbox_id, parsed.message_id)
        if thread_id is not None:
            thread = await session.get(Thread, thread_id)

    if thread is None and key:
        _, was_reply = strip_subject(parsed.subject)
        if was_reply or parsed.in_reply_to or parsed.references:
            query = select(Thread).where(Thread.mailbox_id == mailbox_id, Thread.subject_key == key)
            if timestamp is not None:
                query = query.where(Thread.last_message_at >= timestamp - SUBJECT_FALLBACK_WINDOW)
            thread = await session.scalar(
                query.order_by(Thread.last_message_at.desc().nulls_last()).limit(1)
            )

    if thread is None:
        thread = Thread(mailbox_id=mailbox_id, subject_key=key)
        session.add(thread)

    if provider_thread_id and not thread.provider_thread_id:
        thread.provider_thread_id = provider_thread_id
    if timestamp is not None and (
        thread.last_message_at is None or timestamp > thread.last_message_at
    ):
        thread.last_message_at = timestamp
    await session.flush()
    return thread

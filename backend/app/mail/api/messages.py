"""Read API for mails: inbox list, thread, sanitised body, attachments, read/unread and
flags (archive, move and trash: ``app.mail.api.actions``).

Access goes through the mailbox access rules (``app.mail.access.visible_to``): a
message of a mailbox the user cannot read answers 404, like a missing one. HTML is
sanitised server-side (``app.mail.sanitize``) with external images removed; the client
asks for them explicitly (``/body?external_images=true``). Read/unread is stored at once
and written back to the mail server by the ``mail.write_flags`` job.

Privacy: logs carry IDs only, never subjects, addresses or file names.
"""

import asyncio
import base64
import binascii
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only, selectinload
from sqlalchemy.sql.base import ExecutableOption

from app import audit
from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.events import Event
from app.core.jobs import JobQueue
from app.core.logging import get_logger
from app.mail import access, listing
from app.mail.access import MailboxPermission
from app.mail.api import providers
from app.mail.api.message_schemas import (
    AddressRead,
    AttachmentRead,
    MailboxProviderRead,
    MessageBody,
    MessageDetail,
    MessagePage,
    MessageSummary,
    MessageUpdate,
    ThreadRead,
)
from app.mail.api.router import RegistryDep, StorageDep
from app.mail.models import Attachment, FolderRole, Mailbox, Message
from app.mail.providers.base import Flag
from app.mail.sanitize import sanitize_html
from app.mail.sync.tasks import request_flag_write

log = get_logger(__name__)

# Path prefix of the API as seen by the browser (Caddy and the Vite proxy strip it).
API_PREFIX = "/api"
SNIPPET_LENGTH = listing.SNIPPET_LENGTH
# Newest messages of a thread that are returned.
MAX_THREAD_MESSAGES = 100
# Served inline (for ``cid:`` images in the mail HTML); everything else is a download.
INLINE_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

router = APIRouter(
    prefix="/messages",
    tags=["messages"],
    responses={401: {"description": "Not signed in"}},
)
providers_router = APIRouter(
    prefix="/mailboxes",
    tags=["mailboxes"],
    responses={401: {"description": "Not signed in"}},
)

FlagWriter = Callable[[uuid.UUID], Awaitable[None]]


def get_flag_writer(request: Request) -> FlagWriter:
    queue: JobQueue = request.app.state.job_queue

    async def write(message_id: uuid.UUID) -> None:
        await queue.ensure_open()
        await request_flag_write(message_id)

    return write


DbDep = Annotated[AsyncSession, Depends(get_db)]
FlagWriterDep = Annotated[FlagWriter, Depends(get_flag_writer)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such message"}}


@providers_router.get("/providers")
async def list_mailbox_providers(
    _: CurrentSessionDep, settings: SettingsDep, registry: RegistryDep
) -> list[MailboxProviderRead]:
    """Mailbox types that can be added on this instance and how they are connected.
    OAuth types appear only if their OAuth client is configured."""
    return providers.available(settings, registry)


# -- helpers ------------------------------------------------------------------------------


def _encode_cursor(date: datetime, message_id: uuid.UUID) -> str:
    raw = f"{date.isoformat()}|{message_id}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        date, _, message_id = raw.partition("|")
        return datetime.fromisoformat(date), uuid.UUID(message_id)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ProblemError(422, detail="Invalid cursor.", error_code="invalid_cursor") from None


def _address(value: Any) -> AddressRead | None:
    if not isinstance(value, dict) or not isinstance(value.get("address"), str):
        return None
    name = value.get("name")
    return AddressRead(
        name=name if isinstance(name, str) and name else None, address=value["address"]
    )


def _addresses(values: Sequence[Any] | None) -> list[AddressRead]:
    return [address for value in values or [] if (address := _address(value)) is not None]


def _date_of(message: Message) -> datetime:
    return message.received_at or message.sent_at or message.created_at


async def _message(
    db: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID, *options: ExecutableOption
) -> Message:
    """The message if ``user_id`` may read it, else 404. ``options`` replace the default
    (all columns and the attachments), e.g. to skip the bodies (#188)."""
    message = await db.scalar(
        select(Message)
        .join(Mailbox, Mailbox.id == Message.mailbox_id)
        .where(Message.id == message_id, access.visible_to(user_id))
        .options(*(options or (selectinload(Message.attachments),)))
        # Apply ``options`` also to a message already in the session (e.g. the snippet of
        # ``listing.without_bodies``, which a loaded object would otherwise lack).
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise ProblemError(404, detail="Message not found.")
    return message


def _attachment_url(message_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
    return f"{API_PREFIX}/messages/{message_id}/attachments/{attachment_id}?inline=true"


def _body(message: Message, *, external_images: bool) -> MessageBody:
    if not message.body_html:
        return MessageBody(html=None, blocked_images=0, text=message.body_text)
    by_cid = {
        attachment.content_id: attachment.id
        for attachment in message.attachments
        if attachment.content_id and attachment.content_type in INLINE_IMAGE_TYPES
    }

    def resolve(content_id: str) -> str | None:
        attachment_id = by_cid.get(content_id)
        return _attachment_url(message.id, attachment_id) if attachment_id else None

    safe = sanitize_html(
        message.body_html, allow_external_images=external_images, resolve_cid=resolve
    )
    return MessageBody(html=safe.html, blocked_images=safe.blocked_images, text=message.body_text)


def _summary_fields(message: Message) -> dict[str, Any]:
    flags = set(message.flags or [])
    # List rows carry only the start of the body (``listing.without_bodies``).
    source = message.snippet if message.snippet is not None else message.body_main
    return {
        "id": message.id,
        "mailbox_id": message.mailbox_id,
        "thread_id": message.thread_id,
        "subject": message.subject,
        "sender": _address(message.sender),
        "snippet": " ".join(source[:SNIPPET_LENGTH].split()),
        "date": _date_of(message),
        "unread": Flag.SEEN not in flags,
        "flagged": Flag.FLAGGED in flags,
        "has_attachments": message.has_attachments,
    }


def _detail(message: Message, *, with_body: bool) -> MessageDetail:
    return MessageDetail(
        **_summary_fields(message),
        to=_addresses(message.to),
        cc=_addresses(message.cc),
        reply_to=_addresses(message.reply_to),
        sent_at=message.sent_at,
        body=_body(message, external_images=False) if with_body else None,
        attachments=[
            AttachmentRead(
                id=a.id,
                filename=a.filename,
                content_type=a.content_type,
                size=a.size,
                is_inline=a.is_inline,
            )
            for a in sorted(message.attachments, key=lambda a: (a.is_inline, a.created_at, a.id))
        ],
    )


# -- endpoints ----------------------------------------------------------------------------


@router.get("", responses={422: {"description": "Invalid cursor"}})
async def list_messages(
    current: CurrentSessionDep,
    db: DbDep,
    mailbox_id: uuid.UUID | None = None,
    folder_id: Annotated[
        uuid.UUID | None, Query(description="Default: the inbox folders of all mailboxes")
    ] = None,
    unread: Annotated[bool | None, Query(description="Only unread (true) or read (false)")] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> MessagePage:
    """Messages of the user's mailboxes, newest first, one row per message. Pages with
    ``cursor``; ``total`` counts all matching messages and comes only with the first page
    (without ``cursor``)."""
    mailbox_ids = await listing.readable_mailbox_ids(db, current.user_id, mailbox_id)
    folders = await listing.folder_ids(db, mailbox_ids, folder_id or FolderRole.INBOX)
    read_state = listing.read_state(unread)
    conditions = [listing.in_folders(folders), *read_state]

    before = _decode_cursor(cursor) if cursor is not None else None
    total = await listing.count(db, folders, read_state) if before is None else None
    query = listing.newest_first(mailbox_ids, conditions, before=before, limit=limit + 1)
    rows = list(await db.scalars(query))
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.sort_date, last.id)
    return MessagePage(
        items=[MessageSummary(**_summary_fields(m)) for m in items],
        next_cursor=next_cursor,
        total=total,
    )


@router.get("/{message_id}/thread", responses=NOT_FOUND)
async def get_thread(message_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> ThreadRead:
    """The conversation of a message, oldest first (at most the newest 100 messages).
    Bodies come only for the opened and the newest message, the ones the UI shows
    expanded; the others have ``body: null`` and are loaded with ``/body`` (#210). HTML
    comes sanitised with external images removed."""
    without_bodies = (*listing.without_bodies(), selectinload(Message.attachments))
    message = await _message(db, current.user_id, message_id, *without_bodies)
    messages = [message]
    if message.thread_id is not None:
        newest = list(
            await db.scalars(
                select(Message)
                .where(
                    Message.thread_id == message.thread_id,
                    Message.mailbox_id == message.mailbox_id,
                )
                .options(*without_bodies)
                .order_by(*listing.NEWEST_FIRST)
                .limit(MAX_THREAD_MESSAGES)
            )
        )
        if message not in newest:
            newest[-1:] = [message]
        messages = sorted(newest, key=lambda m: (_date_of(m), m.id))
    expanded = {message, messages[-1]}
    for shown in expanded:
        await db.refresh(shown, ["body_html", "body_text"])
    # Sanitising HTML is CPU-bound: off the event loop (#147). Everything read here is
    # loaded already, so the thread never touches the session.
    details = await asyncio.to_thread(
        lambda: [_detail(m, with_body=m in expanded) for m in messages]
    )
    return ThreadRead(
        thread_id=message.thread_id,
        mailbox_id=message.mailbox_id,
        subject=messages[0].subject if messages[0].subject else message.subject,
        messages=details,
    )


@router.get("/{message_id}/body", responses=NOT_FOUND)
async def get_message_body(
    message_id: uuid.UUID,
    current: CurrentSessionDep,
    db: DbDep,
    external_images: bool = False,
) -> MessageBody:
    """Sanitised HTML and plain text, e.g. for a message expanded in the thread; with
    ``external_images=true`` remote images are kept (the user chose to load them for this
    message)."""
    message = await _message(
        db,
        current.user_id,
        message_id,
        load_only(Message.id, Message.mailbox_id, Message.body_html, Message.body_text),
        selectinload(Message.attachments),
    )
    return await asyncio.to_thread(_body, message, external_images=external_images)


@router.patch("/{message_id}", responses={**NOT_FOUND, 403: {"description": "Read-only mailbox"}})
async def update_message(
    message_id: uuid.UUID,
    body: MessageUpdate,
    current: CurrentSessionDep,
    db: DbDep,
    write_flags: FlagWriterDep,
) -> MessageSummary:
    """Mark read or unread, flag or unflag. Stored at once, written back to the server by
    a job. Needs ``act`` on the mailbox (403 ``read_only``): in a shared mailbox the
    state is the mailbox's, so only users assigned with ``act`` may change it."""
    message = await _message(db, current.user_id, message_id, *listing.without_bodies())
    if (
        await access.get_mailbox(db, current.user_id, message.mailbox_id, MailboxPermission.ACT)
        is None
    ):
        raise ProblemError(403, detail="This mailbox is read-only for you.", error_code="read_only")
    # Before any write: flushing the change would expire the snippet of
    # ``listing.without_bodies`` and reading it again would load the body.
    summary = _summary_fields(message)
    before = set(message.flags or [])
    flags = set(before)
    for flag, value in ((Flag.SEEN, body.seen), (Flag.FLAGGED, body.flagged)):
        if value is True:
            flags.add(flag.value)
        elif value is False:
            flags.discard(flag.value)
    if flags != before:
        message.flags = [flag for flag in message.flags or [] if flag in flags] + sorted(
            flags - before
        )
        ids = {"message_id": message.id, "mailbox_id": message.mailbox_id}
        seen = Flag.SEEN.value in flags
        flagged = Flag.FLAGGED.value in flags
        changes = []
        if seen != (Flag.SEEN.value in before):
            changes.append("seen" if seen else "unseen")
        if flagged != (Flag.FLAGGED.value in before):
            changes.append("flagged" if flagged else "unflagged")
            await audit.record(
                db,
                audit.Actor.user(current.user_id),
                audit.AuditAction.MAIL_FLAGGED,
                audit.Target.of(audit.TargetType.MAILBOX, message.mailbox_id),
                {"message_id": message.id, "flagged": flagged},
            )
        # Everybody who reads the mailbox sees the change (shared mailboxes).
        for change in changes:
            await access.publish_to_readers(
                db, message.mailbox_id, Event(type="message.updated", ids=ids, status=change)
            )
        await db.commit()
        try:
            await write_flags(message.id)
        except Exception as exc:
            # The local state is saved; the server catches up on the next change.
            log.warning(
                "mail_flag_write_request_failed",
                message_id=str(message.id),
                error_type=type(exc).__name__,
            )
    summary.update(unread=Flag.SEEN.value not in flags, flagged=Flag.FLAGGED.value in flags)
    return MessageSummary(**summary)


@router.get(
    "/{message_id}/attachments/{attachment_id}",
    response_class=FileResponse,
    responses={**NOT_FOUND, 200: {"content": {"application/octet-stream": {}}}},
)
async def download_attachment(
    message_id: uuid.UUID,
    attachment_id: uuid.UUID,
    current: CurrentSessionDep,
    db: DbDep,
    storage: StorageDep,
    inline: bool = False,
) -> FileResponse:
    """The attachment file. Downloaded (``Content-Disposition: attachment``) unless
    ``inline=true`` and it is a raster image (for ``cid:`` images in the mail)."""
    message = await _message(
        db,
        current.user_id,
        message_id,
        load_only(Message.id, Message.mailbox_id),
        selectinload(Message.attachments),
    )
    attachment: Attachment | None = next(
        (a for a in message.attachments if a.id == attachment_id), None
    )
    if attachment is None:
        raise ProblemError(404, detail="Attachment not found.")
    try:
        path = storage.path(attachment.storage_path)
    except ValueError:
        path = None
    if path is None or not path.is_file():
        log.warning("mail_attachment_file_missing", attachment_id=str(attachment.id))
        raise ProblemError(404, detail="Attachment not found.")
    show_inline = inline and attachment.content_type in INLINE_IMAGE_TYPES
    return FileResponse(
        path,
        media_type=attachment.content_type if show_inline else "application/octet-stream",
        filename=attachment.filename or f"attachment-{attachment.id}",
        content_disposition_type="inline" if show_inline else "attachment",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, no-store",
        },
    )

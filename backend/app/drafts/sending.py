"""Send a draft through the mailbox it answers from (``MailProvider.send``).

Only on the author's request, only for an open draft whose mail still exists, and only
with ``MailboxPermission.ACT`` on the mailbox. The draft row is locked while the message
goes out, so a double click cannot send twice. A successful send is recorded in the audit
log (``mail.sent``: IDs and counts, never addresses, subject or text).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import audit
from app.audit import Actor, AuditAction, Target, TargetType
from app.core.logging import get_logger
from app.drafts.models import DraftStatus, ReplyDraft
from app.drafts.service import DraftStateError, own_draft
from app.mail import access, compose
from app.mail.access import MailboxPermission
from app.mail.models import Mailbox, Message, Thread
from app.mail.providers.base import (
    OutgoingAddress,
    OutgoingReply,
    ProviderError,
    SentMessage,
)
from app.mail.providers.registry import ProviderFactory
from app.mail.sync.engine import credentials_saver, mailbox_config
from app.users.models import User

log = get_logger(__name__)


class SendNotAllowedError(Exception):
    """The user may read but not send from the mailbox (shared mailbox)."""


class SourceMissingError(Exception):
    """The answered mail was deleted."""


class NoRecipientsError(Exception):
    pass


class InvalidDraftError(Exception):
    """An address or the subject cannot be used in a header."""


@dataclass(frozen=True)
class SendOutcome:
    draft: ReplyDraft
    result: SentMessage


def _outgoing(values: list[dict[str, object]]) -> tuple[OutgoingAddress, ...]:
    return tuple(
        OutgoingAddress(address=str(v["address"]), name=str(v["name"]) if v.get("name") else None)
        for v in values
        if v.get("address")
    )


def _sender_name(mailbox: Mailbox, user: User | None) -> str | None:
    if mailbox.owner_user_id is not None and user is not None and user.display_name:
        return user.display_name
    return mailbox.display_name or None


def _body(draft: ReplyDraft, message: Message) -> str:
    body = draft.body.rstrip()
    if not draft.quote_original:
        return body
    text = (message.body_text or "").strip()
    if not text:
        return body
    sender = message.sender or {}
    who = sender.get("name") or sender.get("address") or "?"
    quoted = compose.quote(text, sender=str(who), sent_at=message.sent_at, language=draft.language)
    return f"{body}\n\n{quoted}"


async def send_draft(
    session: AsyncSession,
    user_id: uuid.UUID,
    draft_id: uuid.UUID,
    *,
    provider_factory: ProviderFactory,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SendOutcome:
    """Send the draft and mark it sent (commits). Raises ``DraftNotFoundError``,
    ``DraftStateError``, ``SendNotAllowedError``, ``SourceMissingError``,
    ``NoRecipientsError``, ``InvalidDraftError`` or the provider's ``ProviderError``."""
    draft = await own_draft(session, user_id, draft_id, lock=True)
    if draft.status is not DraftStatus.DRAFT:
        raise DraftStateError
    mailbox = await access.get_mailbox(session, user_id, draft.mailbox_id, MailboxPermission.ACT)
    if mailbox is None:
        raise SendNotAllowedError
    message = await session.get(Message, draft.message_id) if draft.message_id else None
    if message is None:
        raise SourceMissingError
    to, cc = _outgoing(draft.to or []), _outgoing(draft.cc or [])
    if not to:
        raise NoRecipientsError
    user = await session.get(User, user_id)
    sender = OutgoingAddress(address=mailbox.address, name=_sender_name(mailbox, user))
    body = _body(draft, message)
    try:
        raw, message_id = compose.build_reply(
            sender=sender,
            to=to,
            cc=cc,
            subject=draft.subject,
            body=body,
            in_reply_to=message.message_id_header,
            references=compose.references(message.message_id_header, message.references or []),
            date=now(),
        )
    except compose.InvalidAddressError:
        raise InvalidDraftError from None
    thread = await session.get(Thread, message.thread_id) if message.thread_id else None
    reply = OutgoingReply(
        raw=raw,
        sender=sender,
        to=to,
        cc=cc,
        subject=draft.subject,
        body_text=body,
        message_id=message_id,
        in_reply_to_ref=message.remote_ref,
        provider_thread_id=thread.provider_thread_id if thread is not None else None,
        reply_all=draft.reply_all,
    )

    saver = credentials_saver(
        async_sessionmaker(
            session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
        mailbox.id,
    )
    provider = provider_factory(mailbox_config(mailbox, save_credentials=saver))
    try:
        result = await provider.send(reply)
    except ProviderError as exc:
        log.warning(
            "draft_send_failed", draft_id=str(draft.id), mailbox_id=str(mailbox.id), error=exc.code
        )
        raise
    finally:
        await provider.aclose()

    draft.status = DraftStatus.SENT
    draft.sent_at = now()
    draft.sent_message_id = result.message_id or message_id
    draft.updated_at = draft.sent_at
    await audit.record(
        session,
        Actor.user(user_id),
        AuditAction.MAIL_SENT,
        Target.of(TargetType.MAILBOX, mailbox.id),
        {
            "draft_id": draft.id,
            "message_id": message.id,
            "reply_all": draft.reply_all,
            "recipient_count": len(to) + len(cc),
            "refused": result.refused,
            "sent_copy": result.sent_copy_error is None,
        },
    )
    await session.commit()
    log.info(
        "draft_sent",
        draft_id=str(draft.id),
        mailbox_id=str(mailbox.id),
        recipient_count=len(to) + len(cc),
        refused=result.refused,
        sent_copy_error=result.sent_copy_error,
    )
    return SendOutcome(draft=draft, result=result)

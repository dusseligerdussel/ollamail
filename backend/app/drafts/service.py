"""Reply drafts: generate (LLM stream), edit, send, discard.

Access: a draft is visible only to its author and only while they can read its mailbox
(``accessible_mailbox_ids``, in SQL, on every request); anything else behaves like a
missing draft. Generating needs read access to the answered mail; sending additionally
needs ``MailboxPermission.ACT`` (owners; shared mailboxes are read-only for now).

Generation (:meth:`DraftService.generate`) runs as a stream of events like "ask your
inbox": context from the database (thread up to the answered mail, shortened; the user's
settings; optional style examples from the user's *own* sent mails), then the model
streams the body, then the draft is stored with the signature appended. No database
connection is held while the model writes. Prompts, drafts and mail content are never
logged: only IDs, counts, codes and durations.
"""

import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from string import Template
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import (
    ChatMessage,
    CloudLLMDisabledError,
    GenerationOptions,
    LLMError,
    LLMGateway,
    LLMTask,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.ai.llm.context import CHARS_PER_TOKEN, truncate_to_tokens
from app.core.config import Settings
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.drafts.models import DraftSettings, DraftStatus, ReplyDraft
from app.drafts.prompts import (
    EXAMPLES_HEADING,
    NO_INSTRUCTION,
    REPLY_DRAFT,
    STYLE_RULE,
    Block,
    render_blocks,
)
from app.drafts.schemas import (
    DoneEvent,
    DraftRead,
    ErrorEvent,
    Recipient,
    RecipientRead,
    StartEvent,
    TokenEvent,
)
from app.mail import access, compose
from app.mail.access import MailboxPermission, accessible_mailbox_ids
from app.mail.language import detect_language
from app.mail.models import Folder, FolderRole, Mailbox, Message
from app.mail.models import message_folders as message_folders_table
from app.rag.citations import data_tag
from app.users.models import User

log = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
DraftEvent = StartEvent | TokenEvent | DoneEvent | ErrorEvent

_date = func.coalesce(Message.sent_at, Message.received_at, Message.created_at)


class DraftNotFoundError(Exception):
    pass


class MessageNotFoundError(Exception):
    pass


@dataclass
class _Timer:
    started: float = field(default_factory=time.perf_counter)

    def ms(self) -> int:
        return round((time.perf_counter() - self.started) * 1000)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, CloudLLMDisabledError):
        return "llm_cloud_disabled"
    # Before LLMUnavailableError (its base class): reachable, but too slow.
    if isinstance(exc, LLMTimeoutError):
        return "llm_timeout"
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    return "llm_error"


# --- access ------------------------------------------------------------------------------


async def readable_message(
    session: AsyncSession, user_id: uuid.UUID, message_id: uuid.UUID
) -> Message:
    message = await session.scalar(
        select(Message).where(
            Message.id == message_id, Message.mailbox_id.in_(accessible_mailbox_ids(user_id))
        )
    )
    if message is None:
        raise MessageNotFoundError
    return message


async def own_draft(
    session: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID, *, lock: bool = False
) -> ReplyDraft:
    """The draft if it belongs to ``user_id`` and they can still read its mailbox."""
    statement = select(ReplyDraft).where(
        ReplyDraft.id == draft_id,
        ReplyDraft.user_id == user_id,
        ReplyDraft.mailbox_id.in_(accessible_mailbox_ids(user_id)),
    )
    if lock:
        statement = statement.with_for_update()
    draft = await session.scalar(statement)
    if draft is None:
        raise DraftNotFoundError
    return draft


async def can_send(session: AsyncSession, user_id: uuid.UUID, mailbox_id: uuid.UUID) -> bool:
    mailbox = await access.get_mailbox(session, user_id, mailbox_id, MailboxPermission.ACT)
    return mailbox is not None


async def to_read(session: AsyncSession, draft: ReplyDraft, user_id: uuid.UUID) -> DraftRead:
    return DraftRead(
        id=draft.id,
        message_id=draft.message_id,
        mailbox_id=draft.mailbox_id,
        thread_id=draft.thread_id,
        status=draft.status,
        reply_all=draft.reply_all,
        to=[RecipientRead.model_validate(a) for a in draft.to or []],
        cc=[RecipientRead.model_validate(a) for a in draft.cc or []],
        subject=draft.subject,
        body=draft.body,
        quote_original=draft.quote_original,
        instruction=draft.instruction,
        language=draft.language,
        model=draft.model,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
        sent_at=draft.sent_at,
        can_send=await can_send(session, user_id, draft.mailbox_id),
    )


# --- drafts ------------------------------------------------------------------------------


def _addresses(addresses: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"name": a.name, "address": a.address} for a in addresses]


async def _recipients(
    session: AsyncSession, message: Message, reply_all: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mailbox = await session.get(Mailbox, message.mailbox_id)
    own = [mailbox.address] if mailbox is not None else []
    to, cc = compose.reply_recipients(
        sender=message.sender,
        to=message.to or [],
        cc=message.cc or [],
        reply_to=message.reply_to or [],
        own_addresses=own,
        reply_all=reply_all,
    )
    return _addresses(to), _addresses(cc)


async def new_draft(
    session: AsyncSession,
    user_id: uuid.UUID,
    message: Message,
    *,
    reply_all: bool,
    body: str = "",
    draft_id: uuid.UUID | None = None,
) -> ReplyDraft:
    """A new draft answering ``message`` (added to the session, not committed)."""
    to, cc = await _recipients(session, message, reply_all)
    draft = ReplyDraft(
        id=draft_id or uuid7(),
        user_id=user_id,
        mailbox_id=message.mailbox_id,
        thread_id=message.thread_id,
        message_id=message.id,
        status=DraftStatus.DRAFT,
        reply_all=reply_all,
        to=to,
        cc=cc,
        subject=compose.reply_subject(message.subject),
        body=body,
        quote_original=True,
        language=message.language,
    )
    session.add(draft)
    await session.flush()
    await session.refresh(draft)
    return draft


class DraftStateError(Exception):
    """The draft was sent or discarded already."""


async def update_draft(
    session: AsyncSession,
    draft: ReplyDraft,
    *,
    body: str | None = None,
    subject: str | None = None,
    to: Sequence[Recipient] | None = None,
    cc: Sequence[Recipient] | None = None,
    reply_all: bool | None = None,
    quote_original: bool | None = None,
) -> None:
    if draft.status is not DraftStatus.DRAFT:
        raise DraftStateError
    if reply_all is not None and reply_all != draft.reply_all:
        draft.reply_all = reply_all
        if draft.message_id is not None and (to is None or cc is None):
            message = await session.get(Message, draft.message_id)
            if message is not None:
                new_to, new_cc = await _recipients(session, message, reply_all)
                draft.to = new_to if to is None else draft.to
                draft.cc = new_cc if cc is None else draft.cc
    if to is not None:
        draft.to = _addresses(to)
    if cc is not None:
        draft.cc = _addresses(cc)
    if body is not None:
        draft.body = body
    if subject is not None:
        draft.subject = subject
    if quote_original is not None:
        draft.quote_original = quote_original
    draft.updated_at = datetime.now(UTC)


async def discard_draft(draft: ReplyDraft) -> None:
    if draft.status is DraftStatus.SENT:
        raise DraftStateError
    draft.status = DraftStatus.DISCARDED
    draft.updated_at = datetime.now(UTC)


async def list_drafts(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    message_id: uuid.UUID | None = None,
    status: DraftStatus | None = None,
    limit: int = 50,
) -> list[ReplyDraft]:
    statement = select(ReplyDraft).where(
        ReplyDraft.user_id == user_id,
        ReplyDraft.mailbox_id.in_(accessible_mailbox_ids(user_id)),
    )
    if message_id is not None:
        statement = statement.where(ReplyDraft.message_id == message_id)
    if status is not None:
        statement = statement.where(ReplyDraft.status == status)
    statement = statement.order_by(ReplyDraft.updated_at.desc(), ReplyDraft.id.desc())
    return list(await session.scalars(statement.limit(limit)))


async def user_settings(session: AsyncSession, user_id: uuid.UUID) -> DraftSettings:
    """The user's settings (defaults if none are stored; not added to the session)."""
    stored = await session.scalar(select(DraftSettings).where(DraftSettings.user_id == user_id))
    return stored or DraftSettings(user_id=user_id, signature="", style_examples=True)


async def save_settings(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    signature: str | None,
    style_examples: bool | None,
) -> DraftSettings:
    stored = await session.scalar(
        select(DraftSettings).where(DraftSettings.user_id == user_id).with_for_update()
    )
    if stored is None:
        stored = DraftSettings(user_id=user_id, signature="", style_examples=True)
        session.add(stored)
    if signature is not None:
        stored.signature = signature.strip()
    if style_examples is not None:
        stored.style_examples = style_examples
    await session.flush()
    return stored


async def purge_expired(
    session: AsyncSession, retention_days: int, now: datetime | None = None
) -> int:
    """Delete drafts unchanged for ``retention_days`` (0: keep). Does not commit."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    result = await session.execute(delete(ReplyDraft).where(ReplyDraft.updated_at < cutoff))
    return int(result.rowcount)  # type: ignore[attr-defined]


# --- generation ----------------------------------------------------------------------------


def _heading(message: Message) -> str:
    sender = message.sender or {}
    name = sender.get("name") or ""
    address = sender.get("address") or ""
    who = f"{name} <{address}>" if name and address else name or address
    date = message.sent_at or message.received_at
    lines = [f"From: {who}"]
    if date is not None:
        lines.append(f"Date: {date.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Subject: {message.subject}")
    return "\n".join(lines)


def _text(message: Message) -> str:
    return (message.body_main or message.body_text or "").strip()


def _shorten(text: str, chars: int) -> str:
    return truncate_to_tokens(text, int(chars / CHARS_PER_TOKEN))


@dataclass(frozen=True)
class _Context:
    user_name: str
    user_language: str | None
    thread: list[Block]
    examples: list[Block]
    signature: str
    language: str
    today: str


class DraftService:
    def __init__(self, llm: LLMGateway, sessions: SessionFactory, settings: Settings) -> None:
        self._llm = llm
        self._sessions = sessions
        self._settings = settings

    async def _thread(
        self, session: AsyncSession, user_id: uuid.UUID, message: Message
    ) -> list[Message]:
        """The answered mail and the mails before it in its thread, oldest first."""
        limit = self._settings.drafts.thread_messages
        if message.thread_id is None or limit <= 1:
            return [message]
        source_date = message.sent_at or message.received_at or message.created_at
        earlier = list(
            await session.scalars(
                select(Message)
                .where(
                    Message.thread_id == message.thread_id,
                    Message.id != message.id,
                    Message.mailbox_id.in_(accessible_mailbox_ids(user_id)),
                    _date <= source_date,
                )
                .order_by(_date.desc(), Message.id.desc())
                .limit(limit - 1)
            )
        )
        earlier.reverse()
        return [*earlier, message]

    async def _examples(
        self, session: AsyncSession, user_id: uuid.UUID, language: str, answered: uuid.UUID
    ) -> list[Message]:
        """Recent mails the user sent from their *own* mailboxes (never shared ones)."""
        count = self._settings.drafts.style_examples
        if count <= 0:
            return []
        own_sent = (
            select(message_folders_table.c.message_id)
            .join(Folder, Folder.id == message_folders_table.c.folder_id)
            .join(Mailbox, Mailbox.id == Folder.mailbox_id)
            .where(Folder.role == FolderRole.SENT, Mailbox.owner_user_id == user_id)
        )
        return list(
            await session.scalars(
                select(Message)
                .join(Mailbox, Mailbox.id == Message.mailbox_id)
                .where(
                    Message.id.in_(own_sent),
                    Message.id != answered,
                    Mailbox.owner_user_id == user_id,
                    func.lower(Message.sender["address"].astext) == func.lower(Mailbox.address),
                    Message.language == language,
                    func.length(Message.body_main) > 0,
                )
                .order_by(_date.desc())
                .limit(count)
            )
        )

    async def _context(
        self, session: AsyncSession, user_id: uuid.UUID, message: Message
    ) -> _Context:
        drafts = self._settings.drafts
        user = await session.get(User, user_id)
        user_language = user.language if user else None
        language = (
            message.language or detect_language(_text(message)) or user_language or "en"
        ).lower()
        thread = await self._thread(session, user_id, message)
        blocks = [
            Block(
                heading=_heading(m),
                content=_shorten(_text(m), drafts.message_chars),
                latest=m.id == message.id,
            )
            for m in thread
        ]
        preferences = await user_settings(session, user_id)
        examples: list[Block] = []
        if preferences.style_examples:
            examples = [
                Block(
                    heading=f"Subject: {m.subject}",
                    content=_shorten(_text(m), drafts.style_example_chars),
                    example=True,
                )
                for m in await self._examples(session, user_id, language, message.id)
            ]
        return _Context(
            user_name=(user.display_name if user else "") or "",
            user_language=user_language,
            thread=blocks,
            examples=examples,
            signature=preferences.signature,
            language=language,
            today=datetime.now(UTC).date().isoformat(),
        )

    def _prompt(self, context: _Context, instruction: str | None) -> list[ChatMessage]:
        tag = data_tag()
        lang = REPLY_DRAFT.language_for(context.user_language or context.language)
        thread = render_blocks(tag, context.thread)
        examples = ""
        style = ""
        if context.examples:
            # Numbering continues after the thread, so every block has a unique number.
            blocks = render_blocks(tag, context.examples, start=len(context.thread) + 1)
            heading = Template(EXAMPLES_HEADING[lang]).substitute(user=context.user_name)
            examples = f"{heading}{blocks}\n"
            style = Template(STYLE_RULE[lang]).substitute(user=context.user_name)
        return REPLY_DRAFT.render(
            lang,
            user=context.user_name,
            today=context.today,
            tag=tag,
            reply_language=context.language,
            style=style,
            thread=thread,
            examples=examples,
            instruction=instruction or NO_INSTRUCTION[lang],
        )

    async def generate(
        self,
        user_id: uuid.UUID,
        message_id: uuid.UUID,
        *,
        instruction: str | None = None,
        reply_all: bool = False,
        draft_id: uuid.UUID | None = None,
    ) -> AsyncIterator[DraftEvent]:
        """Stream a generated draft; the last event is ``done`` (stored) or ``error``.

        Raises :class:`MessageNotFoundError` or :class:`DraftNotFoundError` before the
        first event if the mail is not readable or the draft is not the user's open draft
        of that mail.
        """
        timer = _Timer()
        async with self._sessions() as session:
            message = await readable_message(session, user_id, message_id)
            if draft_id is not None:
                draft = await own_draft(session, user_id, draft_id)
                if draft.message_id != message.id or draft.status is not DraftStatus.DRAFT:
                    raise DraftNotFoundError
            context = await self._context(session, user_id, message)
        messages = self._prompt(context, instruction)

        start = StartEvent(draft_id=draft_id or uuid7())
        yield start

        parts: list[str] = []
        ttft_ms: int | None = None
        model = (await self._llm.assignment(LLMTask.REPLY_DRAFT)).model
        try:
            async for chunk in self._llm.stream(
                LLMTask.REPLY_DRAFT,
                messages,
                prompt_version=REPLY_DRAFT.id,
                options=GenerationOptions(max_tokens=self._settings.drafts.max_tokens),
            ):
                if not chunk:
                    continue
                if ttft_ms is None:
                    ttft_ms = timer.ms()
                parts.append(chunk)
                yield TokenEvent(text=chunk)
        except LLMError as exc:
            code = _error_code(exc)
            log.warning("draft_generation_failed", error_type=type(exc).__name__, code=code)
            yield ErrorEvent(code=code)
            return

        body = "".join(parts).strip()
        if context.signature:
            suffix = f"\n\n{context.signature}"
            body += suffix
            yield TokenEvent(text=suffix)

        async with self._sessions() as session:
            stored = await self._store(
                session,
                user_id,
                message_id,
                start.draft_id,
                is_new=draft_id is None,
                body=body,
                reply_all=reply_all,
                instruction=instruction,
                language=context.language,
                model=model,
            )
            if stored is None:
                yield ErrorEvent(code="draft_gone")
                return
            read = await to_read(session, stored, user_id)
            await session.commit()
        log.info(
            "draft_generated",
            draft_id=str(start.draft_id),
            thread_messages=len(context.thread),
            style_examples=len(context.examples),
            regenerated=draft_id is not None,
            ttft_ms=ttft_ms,
            total_ms=timer.ms(),
        )
        yield DoneEvent(draft=read, ttft_ms=ttft_ms)

    async def _store(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        message_id: uuid.UUID,
        draft_id: uuid.UUID,
        *,
        is_new: bool,
        body: str,
        reply_all: bool,
        instruction: str | None,
        language: str,
        model: str,
    ) -> ReplyDraft | None:
        """Store the generated text; ``None`` if mail or draft vanished meanwhile (or the
        user lost access)."""
        try:
            message = await readable_message(session, user_id, message_id)
            if is_new:
                draft = await new_draft(
                    session, user_id, message, reply_all=reply_all, draft_id=draft_id
                )
            else:
                draft = await own_draft(session, user_id, draft_id, lock=True)
                if draft.status is not DraftStatus.DRAFT:
                    return None
                if draft.reply_all != reply_all:
                    await update_draft(session, draft, reply_all=reply_all)
        except (MessageNotFoundError, DraftNotFoundError):
            return None
        draft.body = body
        draft.instruction = instruction
        draft.language = language
        draft.model = model
        draft.prompt_version = REPLY_DRAFT.id
        draft.updated_at = datetime.now(UTC)
        await session.flush()
        await session.refresh(draft)
        return draft

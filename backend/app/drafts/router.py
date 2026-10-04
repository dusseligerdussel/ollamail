"""API of reply drafts: generate (SSE), write, edit, send, discard; drafting settings.

``POST /drafts/generate`` answers as ``text/event-stream`` like ``POST /rag/ask``: each SSE
``event`` is the event type, ``data`` the JSON-encoded event (``start``, ``token``...,
then ``done`` with the stored draft, or ``error``). Clients read it with ``fetch``.

Nothing is sent without ``POST /drafts/{id}/send``. Drafts of other users and of mailboxes
the user can no longer read answer 404.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMGateway, get_llm
from app.ai.llm.user_limits import LLM_BUSY, user_llm_slot
from app.auth.dependencies import CurrentSessionDep
from app.core.config import Settings
from app.core.db import Database, get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.drafts import service
from app.drafts.models import DraftStatus
from app.drafts.schemas import (
    DraftCreate,
    DraftGenerate,
    DraftRead,
    DraftSettingsRead,
    DraftSettingsUpdate,
    DraftStreamEvent,
    DraftUpdate,
    ErrorEvent,
)
from app.drafts.sending import (
    InvalidDraftError,
    NoRecipientsError,
    SendNotAllowedError,
    SourceMissingError,
    send_draft,
)
from app.drafts.service import (
    DraftEvent,
    DraftNotFoundError,
    DraftService,
    DraftStateError,
    MessageNotFoundError,
    SessionFactory,
)
from app.mail.api.router import RegistryDep
from app.mail.providers.base import (
    ConfigurationError,
    ConnectionFailedError,
    ProviderError,
)
from app.rag.router import _inline_schema

log = get_logger(__name__)

router = APIRouter(
    prefix="/drafts",
    tags=["drafts"],
    responses={401: {"description": "Not signed in"}},
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such draft"}}
STREAM_SCHEMA = _inline_schema(TypeAdapter(DraftStreamEvent))

DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_session_factory(request: Request) -> SessionFactory:
    """Sessions opened per step of a generation, so none is held while the model writes."""
    database: Database = request.app.state.database
    return database.sessionmaker


def get_draft_service(
    request: Request,
    llm: Annotated[LLMGateway, Depends(get_llm)],
    sessions: Annotated[SessionFactory, Depends(get_session_factory)],
) -> DraftService:
    settings: Settings = request.app.state.settings
    return DraftService(llm, sessions, settings)


def _not_found() -> ProblemError:
    return ProblemError(404, detail="Draft not found.")


def _message_not_found() -> ProblemError:
    return ProblemError(404, detail="Message not found.", error_code="message_not_found")


def _state_conflict() -> ProblemError:
    return ProblemError(
        409, detail="The draft was already sent or discarded.", error_code="draft_closed"
    )


async def _own(db: AsyncSession, user_id: uuid.UUID, draft_id: uuid.UUID) -> Any:
    try:
        return await service.own_draft(db, user_id, draft_id)
    except DraftNotFoundError:
        raise _not_found() from None


def _sse(event: DraftEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _stream(events: AsyncIterator[DraftEvent]) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield _sse(event)
    except Exception as exc:
        log.error("draft_stream_failed", error_type=type(exc).__name__)
        yield _sse(ErrorEvent(code="internal"))


# -- settings (before ``/{draft_id}``) -----------------------------------------------------


def _settings_read(stored: Any, settings: Settings) -> DraftSettingsRead:
    return DraftSettingsRead(
        signature=stored.signature or "",
        style_examples=bool(stored.style_examples),
        style_examples_available=settings.drafts.style_examples > 0,
    )


@router.get("/settings")
async def get_draft_settings(
    current: CurrentSessionDep, db: DbDep, request: Request
) -> DraftSettingsRead:
    """Own drafting settings: signature and style examples."""
    stored = await service.user_settings(db, current.user_id)
    return _settings_read(stored, request.app.state.settings)


@router.put("/settings")
async def update_draft_settings(
    body: DraftSettingsUpdate, current: CurrentSessionDep, db: DbDep, request: Request
) -> DraftSettingsRead:
    stored = await service.save_settings(
        db, current.user_id, signature=body.signature, style_examples=body.style_examples
    )
    await db.commit()
    return _settings_read(stored, request.app.state.settings)


# -- drafts --------------------------------------------------------------------------------


@router.get("")
async def list_drafts(
    current: CurrentSessionDep,
    db: DbDep,
    message_id: uuid.UUID | None = None,
    status_: Annotated[DraftStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DraftRead]:
    """Own drafts, most recently changed first; optionally of one mail or one status."""
    drafts = await service.list_drafts(
        db, current.user_id, message_id=message_id, status=status_, limit=limit
    )
    return [await service.to_read(db, draft, current.user_id) for draft in drafts]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "No such message"}},
)
async def create_draft(body: DraftCreate, current: CurrentSessionDep, db: DbDep) -> DraftRead:
    """Start a draft written by hand: recipients and subject are filled in from the mail."""
    try:
        message = await service.readable_message(db, current.user_id, body.message_id)
    except MessageNotFoundError:
        raise _message_not_found() from None
    draft = await service.new_draft(
        db, current.user_id, message, reply_all=body.reply_all, body=body.body
    )
    read = await service.to_read(db, draft, current.user_id)
    await db.commit()
    return read


@router.post(
    "/generate",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-Sent Events stream; `data` of each event is one of these",
            "content": {"text/event-stream": {"schema": STREAM_SCHEMA}},
        },
        404: {"description": "No such message or draft"},
        **LLM_BUSY,
    },
    dependencies=[Depends(user_llm_slot)],
)
async def generate_draft(
    body: DraftGenerate,
    current: CurrentSessionDep,
    drafts: Annotated[DraftService, Depends(get_draft_service)],
) -> StreamingResponse:
    """Generate a reply to a mail with the local model; the text is streamed and stored as
    draft when complete. With ``draft_id`` the text of that open draft is replaced.

    The model sees the thread up to the mail (shortened), the instruction, the user's
    signature and, unless switched off, a few of the user's own sent mails as style
    examples. Mail content is passed as data, never as instructions."""
    events = drafts.generate(
        current.user_id,
        body.message_id,
        instruction=body.instruction,
        reply_all=body.reply_all,
        draft_id=body.draft_id,
    )
    try:
        # Runs the preparation up to the first event: a foreign mail or draft is a 404
        # rather than a broken stream.
        first = await anext(events)
    except MessageNotFoundError:
        raise _message_not_found() from None
    except DraftNotFoundError:
        raise _not_found() from None

    async def all_events() -> AsyncIterator[DraftEvent]:
        yield first
        async for event in events:
            yield event

    return StreamingResponse(
        _stream(all_events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{draft_id}", responses=NOT_FOUND)
async def get_draft(draft_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> DraftRead:
    draft = await _own(db, current.user_id, draft_id)
    return await service.to_read(db, draft, current.user_id)


@router.patch(
    "/{draft_id}",
    responses={**NOT_FOUND, 409: {"description": "Draft already sent or discarded"}},
)
async def update_draft(
    draft_id: uuid.UUID, body: DraftUpdate, current: CurrentSessionDep, db: DbDep
) -> DraftRead:
    """Edit text, subject or recipients of an open draft."""
    draft = await _own(db, current.user_id, draft_id)
    try:
        await service.update_draft(
            db,
            draft,
            body=body.body,
            subject=body.subject,
            to=body.to,
            cc=body.cc,
            reply_all=body.reply_all,
            quote_original=body.quote_original,
        )
    except DraftStateError:
        raise _state_conflict() from None
    await db.flush()
    read = await service.to_read(db, draft, current.user_id)
    await db.commit()
    return read


@router.post(
    "/{draft_id}/send",
    responses={
        **NOT_FOUND,
        403: {"description": "Sending from this mailbox is not allowed (shared mailbox)"},
        409: {"description": "Draft already sent or discarded, or the mail was deleted"},
        422: {"description": "No or invalid recipients"},
        502: {"description": "The mail server refused the message (error_code)"},
        503: {"description": "The mail server is unreachable"},
    },
)
async def send(
    draft_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, providers: RegistryDep
) -> DraftRead:
    """Send the draft from the mailbox of the answered mail. The sent copy is kept on the
    server (sent folder) and arrives with the next sync. Recorded in the audit log."""
    try:
        outcome = await send_draft(db, current.user_id, draft_id, provider_factory=providers.create)
    except DraftNotFoundError:
        raise _not_found() from None
    except DraftStateError:
        raise _state_conflict() from None
    except SendNotAllowedError:
        raise ProblemError(
            403, detail="You cannot send from this mailbox.", error_code="read_only"
        ) from None
    except SourceMissingError:
        raise ProblemError(
            409, detail="The mail was deleted.", error_code="message_deleted"
        ) from None
    except NoRecipientsError:
        raise ProblemError(
            422, detail="The draft has no recipient.", error_code="no_recipients"
        ) from None
    except InvalidDraftError:
        raise ProblemError(
            422, detail="A recipient or the subject is invalid.", error_code="invalid_header"
        ) from None
    except ConfigurationError as exc:
        raise ProblemError(
            409, detail="The mailbox is not set up for sending.", error_code=exc.code
        ) from None
    except ConnectionFailedError as exc:
        raise ProblemError(
            503, detail="The mail server is unreachable.", error_code=exc.code
        ) from None
    except ProviderError as exc:
        raise ProblemError(
            502, detail="The mail server did not accept the message.", error_code=exc.code
        ) from None
    return await service.to_read(db, outcome.draft, current.user_id)


@router.post(
    "/{draft_id}/discard",
    responses={**NOT_FOUND, 409: {"description": "Draft already sent"}},
)
async def discard(draft_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> DraftRead:
    """Mark the draft as discarded (kept until the retention period ends)."""
    draft = await _own(db, current.user_id, draft_id)
    try:
        await service.discard_draft(draft)
    except DraftStateError:
        raise _state_conflict() from None
    await db.flush()
    read = await service.to_read(db, draft, current.user_id)
    await db.commit()
    return read


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_draft(draft_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> Response:
    """Delete the draft for good."""
    draft = await _own(db, current.user_id, draft_id)
    await db.delete(draft)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""API of "ask your inbox": ask with a streamed answer (SSE), manage own conversations.

``POST /rag/ask`` answers as ``text/event-stream``: each SSE ``event`` is the event type,
``data`` the JSON-encoded event (``start``, ``filters``, ``sources``, ``token``..., then
``done`` or ``error``). It is a POST (the question is in the body, never in a URL that
could end up in access logs), so clients read it with ``fetch`` rather than
``EventSource``.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMGateway, get_llm
from app.auth.dependencies import CurrentSessionDep
from app.core.config import Settings
from app.core.db import Database, get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.rag import service
from app.rag.rerank import LLMReranker, reranker_enabled
from app.rag.schemas import (
    AskRequest,
    ConversationRead,
    ConversationSummary,
    ErrorEvent,
    RagStreamEvent,
)
from app.rag.service import ConversationNotFoundError, RagEvent, RagService, SessionFactory
from app.search.embedder import GatewayEmbedder

log = get_logger(__name__)

router = APIRouter(
    prefix="/rag",
    tags=["rag"],
    responses={401: {"description": "Not signed in"}},
)

NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such conversation"}}


def _inline_schema(adapter: TypeAdapter[Any]) -> dict[str, Any]:
    """JSON schema without ``$defs`` (references resolved), for an OpenAPI content entry."""
    schema = adapter.json_schema(mode="serialization")
    definitions = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(definitions[ref.removeprefix("#/$defs/")])
            return {key: resolve(value) for key, value in node.items() if key != "mapping"}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


STREAM_SCHEMA = _inline_schema(TypeAdapter(RagStreamEvent))


def get_session_factory(request: Request) -> SessionFactory:
    """Sessions opened per step of an answer, so none is held while the model writes."""
    database: Database = request.app.state.database
    return database.sessionmaker


def get_rag_service(
    request: Request,
    llm: Annotated[LLMGateway, Depends(get_llm)],
    sessions: Annotated[SessionFactory, Depends(get_session_factory)],
) -> RagService:
    settings: Settings = request.app.state.settings
    return RagService(
        llm,
        sessions,
        settings,
        embedder=GatewayEmbedder(llm, settings.search),
        reranker=LLMReranker(llm) if reranker_enabled(settings) else None,
    )


DbDep = Annotated[AsyncSession, Depends(get_db)]


def _sse(event: RagEvent) -> str:
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def _stream(events: AsyncIterator[RagEvent]) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield _sse(event)
    except Exception as exc:
        # The response has started: report the failure as last event (no details).
        log.error("rag_stream_failed", error_type=type(exc).__name__)
        yield _sse(ErrorEvent(code="internal"))


@router.post(
    "/ask",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Server-Sent Events stream; `data` of each event is one of these",
            "content": {"text/event-stream": {"schema": STREAM_SCHEMA}},
        },
        **NOT_FOUND,
    },
)
async def ask(
    body: AskRequest,
    current: CurrentSessionDep,
    rag: Annotated[RagService, Depends(get_rag_service)],
) -> StreamingResponse:
    """Ask a question about the own mails; the answer is streamed with citations.

    Only mails of mailboxes the user may read are searched. Without ``conversation_id`` a
    new conversation is started; the question and the answer are stored when the answer
    is complete.
    """
    events = rag.ask(
        current.user_id,
        body.question,
        conversation_id=body.conversation_id,
        filters=body.filters,
    )
    try:
        # Runs the preparation up to the first event: an unknown or foreign conversation
        # is a 404 rather than a broken stream.
        first = await anext(events)
    except ConversationNotFoundError:
        raise ProblemError(404, detail="Conversation not found.") from None

    async def all_events() -> AsyncIterator[RagEvent]:
        yield first
        async for event in events:
            yield event

    return StreamingResponse(
        _stream(all_events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations")
async def list_conversations(
    current: CurrentSessionDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConversationSummary]:
    """Own conversations, most recently used first."""
    conversations = await service.list_conversations(
        db, current.user_id, limit=limit, offset=offset
    )
    return [ConversationSummary.model_validate(c) for c in conversations]


@router.get("/conversations/{conversation_id}", responses=NOT_FOUND)
async def get_conversation(
    conversation_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> ConversationRead:
    conversation = await service.read_conversation(db, current.user_id, conversation_id)
    if conversation is None:
        raise ProblemError(404, detail="Conversation not found.")
    return conversation


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=NOT_FOUND,
)
async def delete_conversation(
    conversation_id: uuid.UUID, current: CurrentSessionDep, db: DbDep
) -> Response:
    """Delete a conversation with its questions, answers and cited excerpts."""
    if not await service.delete_conversations(db, current.user_id, conversation_id):
        raise ProblemError(404, detail="Conversation not found.")
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/conversations", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_conversations(current: CurrentSessionDep, db: DbDep) -> Response:
    """Delete all own conversations."""
    await service.delete_conversations(db, current.user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

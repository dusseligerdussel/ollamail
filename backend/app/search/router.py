"""API of the classic search: hybrid hits, one per message, with an excerpt.

``POST /search`` (the query is in the body, never in a URL that could end up in access
logs). Access is enforced in SQL by ``app.search.service.search`` through
``app.mail.access.accessible_mailbox_ids`` (own and assigned shared mailboxes).
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.ai.llm import LLMGateway, get_llm
from app.ai.llm.user_limits import optional_user_llm_slot
from app.auth.dependencies import CurrentSessionDep
from app.core.config import Settings
from app.core.db import get_db
from app.mail.access import accessible_mailbox_ids
from app.mail.api.message_schemas import AddressRead
from app.mail.models import Attachment, Message
from app.search import service
from app.search.embedder import Embedder, GatewayEmbedder
from app.search.schemas import SearchHitRead, SearchRequest, SearchResults

router = APIRouter(
    prefix="/search",
    tags=["search"],
    responses={401: {"description": "Not signed in"}},
)

EXCERPT_LENGTH = 320
_TERM = re.compile(r"\w{2,}")


def get_embedder(
    request: Request,
    llm: Annotated[LLMGateway, Depends(get_llm)],
    slot: Annotated[bool, Depends(optional_user_llm_slot)],
) -> Embedder | None:
    """Embedder for the query; ``None`` (full text only) while the user has no free LLM
    slot (#191), so a search is never refused for it."""
    if not slot:
        return None
    settings: Settings = request.app.state.settings
    return GatewayEmbedder(llm, settings.search)


def excerpt(content: str, query: str, length: int = EXCERPT_LENGTH) -> str:
    """About ``length`` characters of ``content`` around the first query term in it,
    cut at word boundaries; the start of ``content`` if no term occurs literally."""
    text = " ".join(content.split())
    if len(text) <= length:
        return text
    folded = text.casefold()
    positions = [
        position for term in _TERM.findall(query.casefold()) if (position := folded.find(term)) >= 0
    ]
    start = max(0, min(positions) - length // 4) if positions else 0
    end = min(len(text), start + length)
    start = max(0, end - length)
    if start > 0:
        space = text.find(" ", start)
        start = space + 1 if 0 <= space < start + 30 else start
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start + length // 2 else end
    return f"{'… ' if start > 0 else ''}{text[start:end]}{' …' if end < len(text) else ''}"


def _address(value: object) -> AddressRead | None:
    if not isinstance(value, dict) or not isinstance(value.get("address"), str):
        return None
    name = value.get("name")
    return AddressRead(
        name=name if isinstance(name, str) and name else None, address=value["address"]
    )


@router.post("")
async def search(
    body: SearchRequest,
    current: CurrentSessionDep,
    db: Annotated[AsyncSession, Depends(get_db)],
    request: Request,
    embedder: Annotated[Embedder | None, Depends(get_embedder)],
) -> SearchResults:
    """Messages matching ``query`` (full text and meaning), best first, one hit per
    message. Only mailboxes the user may read are searched."""
    settings: Settings = request.app.state.settings
    filters = body.filters
    hits = await service.search(
        db,
        current.user_id,
        body.query,
        service.SearchFilters(
            mailbox_ids=filters.mailbox_ids,
            category_ids=filters.category_ids,
            sender=filters.sender or None,
            since=filters.since,
            until=filters.until,
        ),
        embedder=embedder,
        settings=settings.search,
        limit=body.limit,
        per_message=True,
    )
    if not hits:
        return SearchResults(hits=[])

    readable = accessible_mailbox_ids(current.user_id)
    messages = {
        message.id: message
        for message in await db.scalars(
            select(Message)
            .where(Message.id.in_([hit.message_id for hit in hits]))
            .where(Message.mailbox_id.in_(readable))
            .options(
                load_only(
                    Message.id,
                    Message.thread_id,
                    Message.subject,
                    Message.sender,
                    Message.sent_at,
                    Message.received_at,
                    Message.created_at,
                )
            )
        )
    }
    attachment_ids = [hit.attachment_id for hit in hits if hit.attachment_id is not None]
    filenames = (
        dict(
            (
                await db.execute(
                    select(Attachment.id, Attachment.filename).where(
                        Attachment.id.in_(attachment_ids)
                    )
                )
            ).tuples()
        )
        if attachment_ids
        else {}
    )
    results: list[SearchHitRead] = []
    for hit in hits:
        message = messages.get(hit.message_id)
        if message is None:
            continue
        results.append(
            SearchHitRead(
                message_id=hit.message_id,
                mailbox_id=hit.mailbox_id,
                thread_id=message.thread_id,
                subject=message.subject,
                sender=_address(message.sender),
                date=message.received_at or message.sent_at or message.created_at,
                source=hit.source,
                attachment_id=hit.attachment_id,
                attachment_filename=filenames.get(hit.attachment_id) if hit.attachment_id else None,
                excerpt=excerpt(hit.content, body.query),
                score=hit.score,
            )
        )
    return SearchResults(hits=results)

"""Answer questions about the user's mails with citations, and keep the conversations.

:meth:`RagService.ask` runs one question as a stream of events:

1. **Context** (database): earlier turns of the conversation, the user's readable mailboxes
   and visible categories.
2. **Query analysis** (LLM, optional): standalone search query and filters from the
   question; combined with the UI filters (``app.rag.query``). On failure the question is
   searched as typed.
3. **Retrieval** with ``app.search.service.search``: access control happens there, in SQL
   (``accessible_mailbox_ids``), never in the prompt. Optionally reranked.
4. **Answer** (LLM stream): the chunks that fit the context window become numbered
   sources in random-tagged data blocks. :class:`CitationFilter` drops every citation that
   does not name one of them. Without sources no model is called.
5. **Store** question, answer and cited excerpts, then log timings (no content).

No database connection is held while the model generates. Nothing here logs questions,
answers or mail content: only IDs, counts, codes and durations.
"""

import dataclasses
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import (
    ChatMessage,
    CloudLLMDisabledError,
    GenerationOptions,
    LLMError,
    LLMGateway,
    LLMTask,
    LLMUnavailableError,
)
from app.ai.llm.context import CHARS_PER_TOKEN
from app.core.config import Settings
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.access import accessible_mailbox_ids
from app.mail.language import detect_language
from app.mail.models import Message
from app.rag.citations import CitationFilter, DataBlock, data_tag, render_blocks
from app.rag.models import AnswerStatus, RagCitation, RagConversation, RagMessage, RagRole
from app.rag.prompts import NO_SOURCES, RAG_ANSWER, RAG_QUERY
from app.rag.query import (
    QueryAnalysis,
    QueryContext,
    choices_text,
    combine,
    query_context,
    search_filters,
    search_query,
    today,
    weekday,
)
from app.rag.rerank import Reranker
from app.rag.schemas import (
    AppliedFilters,
    CitationRead,
    ConversationMessage,
    ConversationRead,
    DoneEvent,
    ErrorEvent,
    FiltersEvent,
    RagFilters,
    Source,
    SourcesEvent,
    StartEvent,
    TokenEvent,
)
from app.search.embedder import Embedder
from app.search.service import SearchHit, search
from app.users.models import User

log = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
RagEvent = StartEvent | FiltersEvent | SourcesEvent | TokenEvent | DoneEvent | ErrorEvent

TITLE_CHARS = 200
# Earlier answers are shortened to this length in the prompt.
HISTORY_ANSWER_CHARS = 2000
# Room for the tags and attributes of one data block, in characters.
BLOCK_OVERHEAD = 80
# A source shortened to fit the context window keeps at least this many characters.
MIN_SOURCE_CHARS = 200
_MARKERS = re.compile(r"\[\s*\d{1,3}(?:\s*[,;]\s*\d{1,3})*\s*\]")
_NONE = {"en": "(none)", "de": "(keine)"}
# Answer limit of the query analysis (filters and a search query, #132).
ANALYSIS_MAX_TOKENS = 256


class ConversationNotFoundError(Exception):
    pass


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


@dataclass
class _Timer:
    started: float = field(default_factory=time.perf_counter)

    def ms(self) -> int:
        return round((time.perf_counter() - self.started) * 1000)


def _language(question: str, fallback: str | None) -> str:
    return detect_language(question) or fallback or "en"


def _snippet(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, CloudLLMDisabledError):
        return "llm_cloud_disabled"
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    return "llm_error"


async def own_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, *, lock: bool = False
) -> RagConversation | None:
    """The conversation if it belongs to ``user_id`` (others behave like missing ones)."""
    statement = select(RagConversation).where(
        RagConversation.id == conversation_id, RagConversation.user_id == user_id
    )
    if lock:
        statement = statement.with_for_update()
    conversation: RagConversation | None = await session.scalar(statement)
    return conversation


async def withheld_answers(
    session: AsyncSession, user_id: uuid.UUID, answer_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """Answers that cite a mailbox ``user_id`` can no longer read. Their text was written
    from those mails, so it is withheld like the citations (access rule in SQL)."""
    if not answer_ids:
        return set()
    return set(
        await session.scalars(
            select(RagCitation.answer_id)
            .where(
                RagCitation.answer_id.in_(list(answer_ids)),
                RagCitation.mailbox_id.not_in(accessible_mailbox_ids(user_id)),
            )
            .distinct()
        )
    )


async def _history(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, turns: int
) -> list[Turn]:
    if turns < 1:
        return []
    rows = list(
        await session.scalars(
            select(RagMessage)
            .where(RagMessage.conversation_id == conversation_id)
            .order_by(RagMessage.position.desc())
            .limit(turns * 2)
        )
    )
    rows.reverse()
    withheld = await withheld_answers(
        session, user_id, [row.id for row in rows if row.role is RagRole.ASSISTANT]
    )
    history: list[Turn] = []
    question: str | None = None
    for row in rows:
        if row.role is RagRole.USER:
            question = row.content
        elif question is not None and row.id not in withheld:
            history.append(Turn(question, row.content))
            question = None
    return history[-turns:]


class RagService:
    def __init__(
        self,
        llm: LLMGateway,
        sessions: SessionFactory,
        settings: Settings,
        *,
        embedder: Embedder | None,
        reranker: Reranker | None = None,
    ) -> None:
        self._llm = llm
        self._sessions = sessions
        self._settings = settings
        self._embedder = embedder
        self._reranker = reranker

    # --- steps -------------------------------------------------------------------------

    async def _analyse(
        self, question: str, history: Sequence[Turn], context: QueryContext, language: str
    ) -> QueryAnalysis | None:
        if not self._settings.rag.filter_extraction_enabled:
            return None
        day = today(context)
        lang = RAG_QUERY.language_for(language)
        none = _NONE.get(lang, _NONE["en"])
        messages = RAG_QUERY.render(
            language,
            today=day.isoformat(),
            weekday=weekday(day, lang),
            mailboxes=choices_text(context.mailboxes, none),
            categories=choices_text(context.categories, none),
            history="\n".join(f"- {turn.question}" for turn in history) or none,
            question=question,
        )
        try:
            return await self._llm.complete_structured(
                LLMTask.RAG_CHAT,
                messages,
                QueryAnalysis,
                prompt_version=RAG_QUERY.id,
                options=GenerationOptions(max_tokens=ANALYSIS_MAX_TOKENS),
                language=language,
            )
        except LLMError as exc:
            log.warning("rag_query_analysis_failed", error_type=type(exc).__name__)
            return None

    async def _retrieve(
        self, user_id: uuid.UUID, question: str, query: str, filters: RagFilters, language: str
    ) -> tuple[list[SearchHit], bool]:
        limit = self._settings.rag.retrieval_limit
        rerank = self._reranker is not None
        async with self._sessions() as session:
            hits = await search(
                session,
                user_id,
                query,
                search_filters(filters),
                embedder=self._embedder,
                settings=self._settings.search,
                limit=max(limit, self._settings.rag.rerank_candidates) if rerank else limit,
            )
        if self._reranker is not None and len(hits) > 1:
            hits = await self._reranker.rerank(question, hits, limit, language=language)
            return hits, True
        return hits[:limit], False

    def _fit(
        self, hits: Sequence[SearchHit], fixed: Sequence[ChatMessage], context_tokens: int
    ) -> list[SearchHit]:
        """The best chunks that fit next to prompt, history and answer. The best chunk is
        always kept, shortened if needed, so the gateway never has to cut the question."""
        budget = int((context_tokens - self._settings.rag.max_answer_tokens) * CHARS_PER_TOKEN)
        budget -= sum(len(message.content) for message in fixed)
        fitted: list[SearchHit] = []
        for hit in hits:
            size = len(hit.heading) + len(hit.content) + BLOCK_OVERHEAD
            if size > budget:
                if not fitted:
                    room = max(budget - len(hit.heading) - BLOCK_OVERHEAD, MIN_SOURCE_CHARS)
                    fitted.append(dataclasses.replace(hit, content=hit.content[:room]))
                break
            fitted.append(hit)
            budget -= size
        return fitted

    def _source(self, number: int, hit: SearchHit) -> Source:
        return Source(
            number=number,
            message_id=hit.message_id,
            mailbox_id=hit.mailbox_id,
            attachment_id=hit.attachment_id,
            source=hit.source,
            heading=hit.heading,
            snippet=_snippet(hit.content, self._settings.rag.snippet_chars),
        )

    async def _store(
        self,
        user_id: uuid.UUID,
        start: StartEvent,
        is_new: bool,
        question: str,
        filters: AppliedFilters,
        answer: str,
        status: AnswerStatus,
        sources: Sequence[Source],
        cited: Sequence[int],
        model: str | None,
    ) -> bool:
        async with self._sessions() as session:
            position = 0
            if is_new:
                session.add(
                    RagConversation(
                        id=start.conversation_id, user_id=user_id, title=question[:TITLE_CHARS]
                    )
                )
            else:
                # Locked: parallel questions in one conversation get distinct positions.
                conversation = await own_conversation(
                    session, user_id, start.conversation_id, lock=True
                )
                if conversation is None:
                    # Deleted while the answer was generated.
                    return False
                conversation.updated_at = datetime.now(UTC)
                last = await session.scalar(
                    select(func.max(RagMessage.position)).where(
                        RagMessage.conversation_id == conversation.id
                    )
                )
                position = 0 if last is None else last + 1
            session.add_all(
                [
                    RagMessage(
                        id=start.question_id,
                        conversation_id=start.conversation_id,
                        position=position,
                        role=RagRole.USER,
                        content=question,
                        filters=filters.model_dump(mode="json"),
                    ),
                    RagMessage(
                        id=start.answer_id,
                        conversation_id=start.conversation_id,
                        position=position + 1,
                        role=RagRole.ASSISTANT,
                        content=answer,
                        status=status,
                        model=model,
                        prompt_version=RAG_ANSWER.id if model else None,
                    ),
                ]
            )
            await session.flush()
            by_number = {source.number: source for source in sources}
            # Mails deleted while the answer was generated are not cited.
            existing = set(
                await session.scalars(
                    select(Message.id).where(
                        Message.id.in_([by_number[number].message_id for number in cited])
                    )
                )
            )
            session.add_all(
                [
                    RagCitation(
                        answer_id=start.answer_id,
                        number=number,
                        message_id=source.message_id,
                        mailbox_id=source.mailbox_id,
                        attachment_id=source.attachment_id,
                        source=source.source,
                        heading=source.heading,
                        snippet=source.snippet,
                    )
                    for number in cited
                    if (source := by_number[number]).message_id in existing
                ]
            )
            await session.commit()
        return True

    # --- ask ---------------------------------------------------------------------------

    async def ask(
        self,
        user_id: uuid.UUID,
        question: str,
        *,
        conversation_id: uuid.UUID | None = None,
        filters: RagFilters | None = None,
    ) -> AsyncIterator[RagEvent]:
        """Answer ``question`` as a stream of events; the last one is ``done`` or ``error``.

        Raises :class:`ConversationNotFoundError` before the first event if
        ``conversation_id`` is not a conversation of ``user_id``.
        """
        timer = _Timer()
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            history: list[Turn] = []
            if conversation_id is not None:
                if await own_conversation(session, user_id, conversation_id) is None:
                    raise ConversationNotFoundError
                history = await _history(
                    session, user_id, conversation_id, self._settings.rag.history_turns
                )
            context = await query_context(session, user_id, user.timezone if user else None)
        language = _language(question, user.language if user else None)
        display_name = user.display_name if user else ""

        is_new = conversation_id is None
        start = StartEvent(
            conversation_id=conversation_id or uuid7(), question_id=uuid7(), answer_id=uuid7()
        )
        yield start

        analysis = await self._analyse(question, history, context, language)
        applied = combine(filters or RagFilters(), analysis, context)
        yield FiltersEvent(filters=applied)

        hits, reranked = await self._retrieve(
            user_id, question, search_query(question, analysis), applied, language
        )
        retrieval_ms = timer.ms()

        tag = data_tag()

        def answer_prompt(sources_text: str) -> list[ChatMessage]:
            return RAG_ANSWER.render(
                language,
                user=display_name,
                today=today(context).isoformat(),
                tag=tag,
                sources=sources_text,
                question=question,
            )

        prior: list[ChatMessage] = []
        for turn in history:
            prior.append(ChatMessage(role="user", content=turn.question))
            answer_text = _MARKERS.sub("", turn.answer)[:HISTORY_ANSWER_CHARS]
            prior.append(ChatMessage(role="assistant", content=answer_text))

        model: str | None = None
        if hits:
            assignment = await self._llm.assignment(LLMTask.RAG_CHAT)
            model = assignment.model
            hits = self._fit(hits, [*answer_prompt(""), *prior], assignment.context_tokens)
        sources = [self._source(number, hit) for number, hit in enumerate(hits, start=1)]
        yield SourcesEvent(sources=sources)

        parts: list[str] = []
        ttft_ms: int | None = None
        citations = CitationFilter(len(sources))
        if not sources:
            text = NO_SOURCES[RAG_ANSWER.language_for(language)]
            ttft_ms = timer.ms()
            parts.append(text)
            yield TokenEvent(text=text)
        else:
            blocks = [
                DataBlock(number, hit.heading, hit.content)
                for number, hit in enumerate(hits, start=1)
            ]
            system, request = answer_prompt(render_blocks(tag, blocks))
            try:
                async for chunk in self._llm.stream(
                    LLMTask.RAG_CHAT,
                    [system, *prior, request],
                    prompt_version=RAG_ANSWER.id,
                    options=GenerationOptions(max_tokens=self._settings.rag.max_answer_tokens),
                ):
                    piece = citations.feed(chunk)
                    if piece:
                        if ttft_ms is None:
                            ttft_ms = timer.ms()
                        parts.append(piece)
                        yield TokenEvent(text=piece)
            except LLMError as exc:
                code = _error_code(exc)
                log.warning("rag_answer_failed", error_type=type(exc).__name__, code=code)
                yield ErrorEvent(code=code)
                return
            rest = citations.flush()
            if rest:
                parts.append(rest)
                yield TokenEvent(text=rest)

        status = AnswerStatus.ANSWERED if citations.cited else AnswerStatus.NO_EVIDENCE
        stored = await self._store(
            user_id,
            start,
            is_new,
            question,
            applied,
            "".join(parts).strip(),
            status,
            sources,
            citations.cited,
            model,
        )
        log.info(
            "rag_answer_finished",
            conversation_id=str(start.conversation_id),
            status=status.value,
            sources=len(sources),
            cited=len(citations.cited),
            reranked=reranked,
            filters_extracted=len(applied.extracted),
            follow_up=bool(history),
            stored=stored,
            retrieval_ms=retrieval_ms,
            ttft_ms=ttft_ms,
            total_ms=timer.ms(),
        )
        yield DoneEvent(status=status, citations=citations.cited, ttft_ms=ttft_ms)


# --- conversations -------------------------------------------------------------------


async def list_conversations(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> list[RagConversation]:
    """Own conversations, most recently used first."""
    return list(
        await session.scalars(
            select(RagConversation)
            .where(RagConversation.user_id == user_id)
            .order_by(RagConversation.updated_at.desc(), RagConversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )


async def read_conversation(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> ConversationRead | None:
    """A conversation with its messages. Citations of mailboxes the user can no longer read
    are left out, and so is the text of answers that cite them (``withheld``); the access
    rule is applied in SQL, as for the search."""
    conversation = await own_conversation(session, user_id, conversation_id)
    if conversation is None:
        return None
    messages = list(
        await session.scalars(
            select(RagMessage)
            .where(RagMessage.conversation_id == conversation.id)
            .order_by(RagMessage.position)
        )
    )
    citations: dict[uuid.UUID, list[CitationRead]] = {}
    for citation in await session.scalars(
        select(RagCitation)
        .where(
            RagCitation.answer_id.in_([m.id for m in messages]),
            RagCitation.mailbox_id.in_(accessible_mailbox_ids(user_id)),
        )
        .order_by(RagCitation.answer_id, RagCitation.number)
    ):
        citations.setdefault(citation.answer_id, []).append(CitationRead.model_validate(citation))
    withheld = await withheld_answers(
        session, user_id, [m.id for m in messages if m.role is RagRole.ASSISTANT]
    )
    return ConversationRead(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[
            ConversationMessage(
                id=message.id,
                role=message.role,
                content="" if message.id in withheld else message.content,
                withheld=message.id in withheld,
                created_at=message.created_at,
                filters=(
                    AppliedFilters.model_validate(message.filters) if message.filters else None
                ),
                status=message.status,
                citations=[] if message.id in withheld else citations.get(message.id, []),
            )
            for message in messages
        ],
    )


async def delete_conversations(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID | None = None
) -> int:
    """Delete one or all conversations of ``user_id``; returns the number. Does not commit."""
    statement = delete(RagConversation).where(RagConversation.user_id == user_id)
    if conversation_id is not None:
        statement = statement.where(RagConversation.id == conversation_id)
    result = await session.execute(statement)
    return int(result.rowcount)  # type: ignore[attr-defined]


async def purge_expired(
    session: AsyncSession, retention_days: int, now: datetime | None = None
) -> int:
    """Delete conversations unused for ``retention_days`` (0: keep). Does not commit."""
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    result = await session.execute(
        delete(RagConversation).where(RagConversation.updated_at < cutoff)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]

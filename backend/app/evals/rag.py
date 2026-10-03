"""RAG stage: "ask your inbox" end to end, with the real search index.

The mails are stored and indexed (``app.search.service.index_message``: chunking, full
text, embeddings of the configured model) in a PostgreSQL database with pgvector, then
every question runs through ``RagService.ask``: query analysis, hybrid search, optional
reranking, the answer prompt and the citation filter. Everything happens in one
transaction that is rolled back at the end, so the database is left as it was. Use a
separate database for this (the vector column is resized to the embedding model inside
the transaction, which locks the table).

Scores per question: rank of the expected mail among the sources given to the model
(Recall@k, MRR), answer correct by rules (keyword groups; for questions without answer:
no citation or a "found nothing" answer), and optionally the verdict of an LLM judge.
The judge is a second opinion with its own error rate and is reported separately.
"""

import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.llm import LLMError, LLMGateway, LLMTask
from app.core.config import Settings
from app.core.ids import uuid7
from app.evals.dataset import OWNER_ADDRESS, OWNER_NAME, OWNER_TIMEZONE, Dataset, Question
from app.evals.metrics import (
    answer_contains,
    mean_reciprocal_rank,
    normalize,
    recall_at_k,
    source_rank,
)
from app.evals.prompts import EVAL_JUDGE
from app.evals.triage import recipients
from app.mail.models import Mailbox, MailboxType, Message
from app.mail.storage import AttachmentStorage
from app.rag.models import AnswerStatus
from app.rag.rerank import LLMReranker, reranker_enabled
from app.rag.schemas import DoneEvent, ErrorEvent, SourcesEvent, TokenEvent
from app.rag.service import RagService, SessionFactory
from app.search.embedder import GatewayEmbedder
from app.search.service import embedding_dimensions, index_message, resize_embeddings
from app.users.models import User

RECALL_AT = (1, 3, 5)
# Words of an answer that says nothing was found (rule for questions without answer).
NOTHING_FOUND = (
    "nichts",
    "keine e mail",
    "keine mail",
    "keine information",
    "nicht gefunden",
    "no e mail",
    "no email",
    "no mail",
    "nothing",
    "not find",
    "couldn t find",
    "no information",
)


class JudgeVerdict(BaseModel):
    correct: bool
    reason: str = Field(default="", max_length=500)


@dataclass(frozen=True)
class RagOutcome:
    question_id: str
    no_answer: bool
    # Mail ids of the sources given to the model, in order.
    sources: list[str]
    rank: int | None
    cited_expected: bool
    status: str
    correct: bool
    judge: bool | None
    seconds: float
    first_token_ms: int | None
    error: str | None = None


@dataclass
class RagReport:
    outcomes: list[RagOutcome] = field(default_factory=list)
    indexed_mails: int = 0
    index_seconds: float = 0.0
    embedding_model: str | None = None

    def as_dict(self) -> dict[str, object]:
        answerable = [o for o in self.outcomes if not o.no_answer]
        unanswerable = [o for o in self.outcomes if o.no_answer]
        ranks = [o.rank for o in answerable]
        judged = [o for o in self.outcomes if o.judge is not None]
        return {
            "questions": len(self.outcomes),
            "answerable": len(answerable),
            "unanswerable": len(unanswerable),
            "embedding_model": self.embedding_model,
            "indexed_mails": self.indexed_mails,
            "index_seconds": round(self.index_seconds, 1),
            **{f"recall_at_{k}": round(recall_at_k(ranks, k), 4) for k in RECALL_AT},
            "recall_at_sources": round(sum(r is not None for r in ranks) / len(ranks), 4)
            if ranks
            else None,
            "mrr": round(mean_reciprocal_rank(ranks), 4),
            "cited_expected_source": round(
                sum(o.cited_expected for o in answerable) / len(answerable), 4
            )
            if answerable
            else None,
            "answer_correct_rules": round(sum(o.correct for o in answerable) / len(answerable), 4)
            if answerable
            else None,
            "no_answer_correct_rules": round(
                sum(o.correct for o in unanswerable) / len(unanswerable), 4
            )
            if unanswerable
            else None,
            "judge": {
                "judged": len(judged),
                "correct": round(sum(bool(o.judge) for o in judged) / len(judged), 4),
                # Share of questions where judge and rules disagree: the judge's verdicts
                # are uncertain at least to this extent.
                "disagreement_with_rules": round(
                    sum(o.judge != o.correct for o in judged) / len(judged), 4
                ),
            }
            if judged
            else None,
            "first_token_seconds_mean": round(
                sum(o.first_token_ms or 0 for o in self.outcomes) / 1000 / len(self.outcomes), 2
            )
            if self.outcomes
            else 0,
            "seconds_mean": round(sum(o.seconds for o in self.outcomes) / len(self.outcomes), 2)
            if self.outcomes
            else 0,
            "errors": sum(o.error is not None for o in self.outcomes),
            "wrong": [
                {
                    "question": o.question_id,
                    "rank": o.rank,
                    "status": o.status,
                    **({"judge": o.judge} if o.judge is not None else {}),
                    **({"error": o.error} if o.error else {}),
                }
                for o in self.outcomes
                if not o.correct or (not o.no_answer and o.rank is None) or o.error
            ],
        }


def says_nothing_found(answer: str) -> bool:
    text = normalize(answer)
    return any(marker in text for marker in NOTHING_FOUND)


def rule_correct(question: Question, answer: str, status: str) -> bool:
    if question.no_answer:
        return status == AnswerStatus.NO_EVIDENCE or says_nothing_found(answer)
    return answer_contains(answer, question.answer)


async def judge_answer(judge: LLMGateway, question: Question, answer: str) -> bool | None:
    expected = (
        "The e-mails contain no answer to this question."
        if question.no_answer
        else "; ".join(" or ".join(group) for group in question.answer)
    )
    try:
        verdict = await judge.complete_structured(
            LLMTask.RAG_CHAT,
            EVAL_JUDGE.render("en", question=question.question, expected=expected, answer=answer),
            JudgeVerdict,
            prompt_version=EVAL_JUDGE.id,
        )
    except LLMError:
        return None
    return verdict.correct


@dataclass
class EvalInbox:
    """The data set stored and indexed for one invented user (rolled back at the end)."""

    session: AsyncSession
    user_id: uuid.UUID
    # Database id of each stored message -> mail id of the data set.
    mail_ids: dict[uuid.UUID, str]

    def sessions(self) -> SessionFactory:
        @asynccontextmanager
        async def factory() -> AsyncIterator[AsyncSession]:
            yield self.session

        return factory


@asynccontextmanager
async def eval_inbox(
    database_url: str, dataset: Dataset, embedder: GatewayEmbedder, settings: Settings
) -> AsyncIterator[tuple[EvalInbox, float]]:
    """Store and index the mails in a transaction that is rolled back on exit. Yields the
    inbox and the seconds the indexing took."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                model = await embedder.current_model()
                probe = await embedder.embed(["dimension probe"])
                # Match the vector column to the model; rolled back with everything else.
                if len(probe[0]) != await embedding_dimensions(session):
                    await resize_embeddings(session, len(probe[0]), model)
                user = User(
                    # Unique even if the database has users: the row is rolled back anyway.
                    email=f"eval-{uuid.uuid4().hex[:12]}@example.org",
                    display_name=OWNER_NAME,
                    timezone=OWNER_TIMEZONE,
                    language="en",
                )
                session.add(user)
                await session.flush()
                mailbox = Mailbox(
                    type=MailboxType.IMAP,
                    display_name="Eval",
                    address=OWNER_ADDRESS,
                    owner_user_id=user.id,
                )
                session.add(mailbox)
                await session.flush()
                mail_ids: dict[uuid.UUID, str] = {}
                for mail in dataset.mails:
                    to, cc = recipients(mail)
                    message = Message(
                        id=uuid7(),
                        mailbox_id=mailbox.id,
                        remote_ref=mail.id,
                        subject=mail.subject,
                        sender=mail.sender.model_dump(),
                        to=to,
                        cc=cc,
                        headers=mail.headers,
                        sent_at=mail.sent_at,
                        received_at=mail.sent_at,
                        body_text=mail.body,
                        body_main=mail.body,
                        language=mail.language,
                    )
                    session.add(message)
                    mail_ids[message.id] = mail.id
                await session.flush()
                started = time.perf_counter()
                with tempfile.TemporaryDirectory() as data_dir:
                    storage = AttachmentStorage(Path(data_dir))
                    for message_id in mail_ids:
                        await index_message(
                            session,
                            message_id,
                            embedder=embedder,
                            storage=storage,
                            settings=settings.search,
                        )
                seconds = time.perf_counter() - started
                yield EvalInbox(session, user.id, mail_ids), seconds
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


async def run_rag(
    llm: LLMGateway,
    inbox: EvalInbox,
    questions: Sequence[Question],
    settings: Settings,
    *,
    judge: LLMGateway | None = None,
) -> RagReport:
    service = RagService(
        llm,
        inbox.sessions(),
        settings,
        embedder=GatewayEmbedder(llm, settings.search),
        reranker=LLMReranker(llm) if reranker_enabled(settings) else None,
    )
    report = RagReport()
    for question in questions:
        started = time.perf_counter()
        sources: list[str] = []
        parts: list[str] = []
        status = "error"
        cited: list[int] = []
        first_token_ms: int | None = None
        error: str | None = None
        async for event in service.ask(inbox.user_id, question.question):
            if isinstance(event, SourcesEvent):
                sources = [inbox.mail_ids.get(s.message_id, "?") for s in event.sources]
            elif isinstance(event, TokenEvent):
                parts.append(event.text)
            elif isinstance(event, DoneEvent):
                status = event.status.value
                cited = event.citations
                first_token_ms = event.ttft_ms
            elif isinstance(event, ErrorEvent):
                error = event.code
        answer = "".join(parts).strip()
        rank = source_rank(sources, question.sources) if sources else None
        cited_mails = {sources[n - 1] for n in cited if 0 < n <= len(sources)}
        correct = error is None and rule_correct(question, answer, status)
        verdict = (
            await judge_answer(judge, question, answer)
            if judge is not None and error is None
            else None
        )
        report.outcomes.append(
            RagOutcome(
                question_id=question.id,
                no_answer=question.no_answer,
                sources=sources,
                rank=rank,
                cited_expected=bool(cited_mails & set(question.sources)),
                status=status,
                correct=correct,
                judge=verdict,
                seconds=time.perf_counter() - started,
                first_token_ms=first_token_ms,
                error=error,
            )
        )
    return report

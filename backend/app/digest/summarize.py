"""Two-stage summary (map-reduce) that works with small CPU models and large inboxes.

* **Map:** mails are sent in small batches (``OLLAMAIL_DIGEST_MAP_BATCH_SIZE``, bounded by
  the context window of the ``digest`` task) and come back as one note per mail, plus a
  deadline or appointment if the mail names one. A batch the model cannot answer validly
  is split in halves and retried; a single mail that still fails falls back to a note
  built from sender and subject. So one confusing mail never loses the whole digest.
* **Condense:** if the notes do not fit into one context window, groups of notes are
  merged into fewer notes (references are kept), repeatedly if needed.
* **Reduce:** the notes, most important first, become the spoken summary with ``[n]``
  references to the mails. References to notes that do not exist are dropped; an answer
  without any valid reference is asked for again once (small models often leave them
  out, #171).

The context window comes from the gateway (``LLMGateway.assignment``), so the same code
fits an 8k-token 3B model on a CPU and a 32k-token model on a GPU server. Mail content is
only sent to the model, never logged.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from app.ai.llm import ChatMessage, GenerationOptions, LLMError, LLMGateway, LLMOutputError, LLMTask
from app.ai.llm.context import estimate_messages_tokens, estimate_tokens, truncate_to_tokens
from app.core.config import DigestSettings
from app.core.logging import get_logger
from app.digest import texts
from app.digest.content import MailItem
from app.digest.models import DigestLength
from app.digest.prompts import (
    DIGEST_CONDENSE,
    DIGEST_MAP,
    DIGEST_REDUCE,
    REDUCE_MISSING_REFERENCES,
)
from app.digest.texts import DigestLanguage

log = get_logger(__name__)

# Target length of the model's part of the script, in words, and the answer budget.
WORDS = {DigestLength.SHORT: 90, DigestLength.NORMAL: 220}
ANSWER_TOKENS = {DigestLength.SHORT: 400, DigestLength.NORMAL: 900}
# Answer tokens per mail in the map step.
MAP_TOKENS_PER_MAIL = 120
# Rounds of condensing before the gateway's truncation takes over.
MAX_CONDENSE_ROUNDS = 3
# Share of the context kept free for estimation errors.
SAFETY = 0.9

PROMPT_VERSION = f"{DIGEST_MAP.id}+{DIGEST_REDUCE.id}"

# ``[3]``, ``[2, 7]`` and the variants small models write (``[ 3 ]``, ``[^3]``, ``[#3]``,
# ``[2; 7]``); ``clean_answer`` normalises them to ``[2, 7]``.
REFERENCE = re.compile(r"\s*\[\s*[#^]?(\d+(?:\s*[,;]\s*[#^]?\d+)*)\s*\]")
_NUMBER = re.compile(r"\d+")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")
_BULLET = re.compile(r"^\s*(?:[-*•]\s+|\d{1,2}[.)]\s+)")


class MapItem(BaseModel):
    ref: int
    summary: str
    deadline: str | None = None


class MapAnswer(BaseModel):
    items: list[MapItem]


@dataclass(frozen=True, slots=True)
class Note:
    refs: tuple[int, ...]
    text: str

    def line(self) -> str:
        if not self.refs:
            return self.text
        return f"{self.text} [{', '.join(str(ref) for ref in self.refs)}]"


@dataclass(frozen=True, slots=True)
class Summary:
    text: str
    model: str | None


def references(text: str) -> list[int]:
    """All ``[n]`` reference numbers in ``text``, in order of first appearance."""
    found: dict[int, None] = {}
    for match in REFERENCE.finditer(text):
        for number in _NUMBER.findall(match.group(1)):
            found.setdefault(int(number), None)
    return list(found)


def _keep_refs(text: str, valid: set[int]) -> str:
    """Drop reference numbers the model invented."""

    def replace(match: re.Match[str]) -> str:
        refs = [n for n in (int(x) for x in _NUMBER.findall(match.group(1))) if n in valid]
        return f" [{', '.join(str(n) for n in dict.fromkeys(refs))}]" if refs else ""

    return REFERENCE.sub(replace, text)


def clean_answer(text: str, valid: set[int]) -> str:
    """Plain paragraphs from the model's answer: no reasoning blocks, headings or bullets,
    only references to existing notes."""
    text = _THINK.sub("", text)
    lines = [
        _BULLET.sub("", line).strip() for line in text.splitlines() if not _HEADING_LINE.match(line)
    ]
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line.replace("**", "").replace("__", ""))
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(_keep_refs(p, valid) for p in paragraphs).strip()


def _has_words(text: str) -> bool:
    return sum(char.isalpha() for char in text) >= 3


class Summarizer:
    def __init__(
        self,
        llm: LLMGateway,
        settings: DigestSettings,
        *,
        language: DigestLanguage,
        length: DigestLength,
        user_label: str,
    ) -> None:
        self.llm = llm
        self.settings = settings
        self.language = language
        self.length = length
        self.user_label = user_label
        self.model: str | None = None
        self.map_fallbacks = 0
        self.reference_retries = 0
        self.calls = 0

    # -- budget --------------------------------------------------------------------------

    async def _context_tokens(self) -> int:
        return (await self.llm.assignment(LLMTask.DIGEST)).context_tokens

    # -- map -----------------------------------------------------------------------------

    def _mail_block(self, ref: int, mail: MailItem, max_tokens: int) -> str:
        header = f"[{ref}] From: {mail.sender}\nSubject: {mail.subject}\n"
        if mail.category:
            header += f"Category: {mail.category}\n"
        body = truncate_to_tokens(mail.body.strip(), max(50, max_tokens - estimate_tokens(header)))
        return header + "\n" + body

    def _map_budget(self, context: int) -> tuple[int, int]:
        """Prompt tokens for the mails of one batch, and at most per mail."""
        system = DIGEST_MAP.render(self.language, user=self.user_label, mails="")
        fixed = estimate_messages_tokens(system) + 400  # plus JSON schema instructions
        size = self.settings.map_batch_size
        budget = int(context * SAFETY) - fixed - MAP_TOKENS_PER_MAIL * size
        return budget, max(100, budget // size)

    def _map_batches(
        self, mails: Sequence[MailItem], budget: int, per_mail: int
    ) -> list[list[tuple[int, MailItem]]]:
        size = self.settings.map_batch_size
        batches: list[list[tuple[int, MailItem]]] = []
        current: list[tuple[int, MailItem]] = []
        used = 0
        for ref, mail in enumerate(mails, start=1):
            tokens = min(per_mail, estimate_tokens(self._mail_block(ref, mail, per_mail)))
            if current and (len(current) >= size or used + tokens > budget):
                batches.append(current)
                current, used = [], 0
            current.append((ref, mail))
            used += tokens
        if current:
            batches.append(current)
        return batches

    def _fallback(self, mail: MailItem) -> str:
        self.map_fallbacks += 1
        return texts.fallback_sentence(mail.sender, mail.subject, self.language)

    async def _map(self, batch: list[tuple[int, MailItem]], per_mail: int) -> dict[int, str]:
        mails = "\n\n---\n\n".join(self._mail_block(ref, mail, per_mail) for ref, mail in batch)
        messages = DIGEST_MAP.render(self.language, user=self.user_label, mails=mails)
        options = GenerationOptions(
            temperature=0.2, max_tokens=MAP_TOKENS_PER_MAIL * len(batch) + 100
        )
        self.calls += 1
        try:
            answer = await self.llm.complete_structured(
                LLMTask.DIGEST,
                messages,
                MapAnswer,
                prompt_version=DIGEST_MAP.id,
                options=options,
                language=self.language,
            )
        except LLMOutputError:
            if len(batch) == 1:
                return {batch[0][0]: self._fallback(batch[0][1])}
            middle = len(batch) // 2
            first = await self._map(batch[:middle], per_mail)
            return first | await self._map(batch[middle:], per_mail)
        deadline_label = "Frist" if self.language == "de" else "deadline"
        notes: dict[int, str] = {}
        by_ref = {item.ref: item for item in answer.items}
        for ref, mail in batch:
            item = by_ref.get(ref)
            summary = " ".join(item.summary.split()) if item is not None else ""
            if not _has_words(summary):
                notes[ref] = self._fallback(mail)
                continue
            deadline = " ".join((item.deadline or "").split()) if item is not None else ""
            if deadline and deadline.lower() not in {"null", "none", "-"}:
                summary = f"{summary.rstrip('.')} ({deadline_label}: {deadline})."
            notes[ref] = summary
        return notes

    async def map(self, mails: Sequence[MailItem]) -> list[Note]:
        budget, per_mail = self._map_budget(await self._context_tokens())
        notes: dict[int, str] = {}
        for batch in self._map_batches(mails, budget, per_mail):
            notes |= await self._map(batch, per_mail)
        return [Note((ref,), notes[ref]) for ref in sorted(notes)]

    # -- condense ------------------------------------------------------------------------

    def _answer_tokens(self, context: int) -> int:
        return min(ANSWER_TOKENS[self.length], context // 4)

    def _reduce_budget(self, context: int) -> int:
        system = DIGEST_REDUCE.render(self.language, words="000", notes="")
        fixed = estimate_messages_tokens(system) + self._answer_tokens(context)
        return max(context // 4, int(context * SAFETY) - fixed)

    async def _condense_group(self, group: list[Note], valid: set[int], context: int) -> list[Note]:
        count = max(1, len(group) // 3)
        messages = DIGEST_CONDENSE.render(
            self.language, count=str(count), notes="\n".join(note.line() for note in group)
        )
        self.calls += 1
        result = await self.llm.complete(
            LLMTask.DIGEST,
            messages,
            prompt_version=DIGEST_CONDENSE.id,
            options=GenerationOptions(temperature=0.2, max_tokens=self._answer_tokens(context)),
        )
        condensed: list[Note] = []
        for line in clean_answer(result.content, valid).split("\n"):
            refs = tuple(ref for ref in references(line) if ref in valid)
            text = REFERENCE.sub("", line).strip()
            if _has_words(text):
                condensed.append(Note(refs, text))
        if not condensed:
            # Unusable answer: keep the notes, shortened.
            return [Note(note.refs, note.text[:120]) for note in group]
        return condensed

    async def condense(self, notes: list[Note], context: int, valid: set[int]) -> list[Note]:
        budget = self._reduce_budget(context)
        for _ in range(MAX_CONDENSE_ROUNDS):
            if estimate_tokens("\n".join(note.line() for note in notes)) <= budget:
                break
            # Groups of at most half the budget, so the answer fits next to them.
            groups: list[list[Note]] = [[]]
            used = 0
            for note in notes:
                tokens = estimate_tokens(note.line()) + 1
                if groups[-1] and used + tokens > budget // 2:
                    groups.append([])
                    used = 0
                groups[-1].append(note)
                used += tokens
            condensed: list[Note] = []
            for group in groups:
                condensed += await self._condense_group(group, valid, context)
            if len(condensed) >= len(notes):
                break
            notes = condensed
        return notes

    # -- reduce --------------------------------------------------------------------------

    async def reduce(self, notes: list[Note], valid: set[int], context: int) -> str:
        messages = DIGEST_REDUCE.render(
            self.language,
            words=str(WORDS[self.length]),
            notes="\n".join(note.line() for note in notes),
        )
        options = GenerationOptions(temperature=0.3, max_tokens=self._answer_tokens(context))
        self.calls += 1
        result = await self.llm.complete(
            LLMTask.DIGEST, messages, prompt_version=DIGEST_REDUCE.id, options=options
        )
        self.model = result.model
        text = clean_answer(result.content, valid)
        if not _has_words(text):
            # Unusable answer: read the notes instead.
            return " ".join(note.line() for note in notes)
        if not references(text):
            text = await self._ask_for_references(messages, result.content, valid, options, text)
        return text

    async def _ask_for_references(
        self,
        messages: list[ChatMessage],
        answer: str,
        valid: set[int],
        options: GenerationOptions,
        text: str,
    ) -> str:
        """Ask once more for an answer with references; keep ``text`` if that fails."""
        self.reference_retries += 1
        self.calls += 1
        follow_up = [
            *messages,
            ChatMessage(role="assistant", content=answer),
            ChatMessage(
                role="user",
                content=REDUCE_MISSING_REFERENCES.get(
                    self.language, REDUCE_MISSING_REFERENCES["en"]
                ),
            ),
        ]
        try:
            result = await self.llm.complete(
                LLMTask.DIGEST, follow_up, prompt_version=DIGEST_REDUCE.id, options=options
            )
        except LLMError:
            return text
        again = clean_answer(result.content, valid)
        return again if _has_words(again) and references(again) else text

    async def summarize(self, mails: Sequence[MailItem]) -> Summary:
        if not mails:
            return Summary("", None)
        valid = set(range(1, len(mails) + 1))
        notes = await self.map(mails)
        context = await self._context_tokens()
        notes = await self.condense(notes, context, valid)
        text = await self.reduce(notes, valid, context)
        log.info(
            "digest_summarized",
            mails=len(mails),
            notes=len(notes),
            llm_calls=self.calls,
            map_fallbacks=self.map_fallbacks,
            reference_retries=self.reference_retries,
            references=len(references(text)),
            chars=len(text),
        )
        return Summary(text, self.model)

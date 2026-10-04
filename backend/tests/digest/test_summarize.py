"""Map-reduce summary with a fake LLM: batching, fallbacks, condensing, answer cleanup."""

import uuid
from datetime import UTC, datetime

import pytest

from app.ai.llm import LLMTask
from app.core.config import DigestSettings
from app.digest.content import MailItem
from app.digest.models import DigestLength
from app.digest.prompts import DIGEST_REDUCE, REDUCE_MISSING_REFERENCES
from app.digest.summarize import Summarizer, clean_answer, references
from tests.digest.conftest import FakeLLM, make_llm


def mails(count: int, body: str = "Please review the attached draft.") -> list[MailItem]:
    return [
        MailItem(
            message_id=uuid.uuid4(),
            mailbox_id=uuid.uuid4(),
            sender=f"Sender {n}",
            subject=f"Subject {n}",
            body=body,
            received_at=datetime(2026, 10, 1, 8, n % 60, tzinfo=UTC),
            category="important",
            priority=2,
        )
        for n in range(1, count + 1)
    ]


def summarizer(llm: FakeLLM, **settings: object) -> Summarizer:
    return Summarizer(
        llm.gateway,
        DigestSettings.model_validate(settings),
        language="en",
        length=DigestLength.NORMAL,
        user_label="Erika Example",
    )


async def test_maps_in_small_batches_and_reduces_once(fake_llm: FakeLLM) -> None:
    summary = await summarizer(fake_llm, map_batch_size=4).summarize(mails(10))

    assert fake_llm.provider.kinds == ["map", "map", "map", "reduce"]
    assert summary.text == "The mails are summarised here [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]."
    assert summary.model == "chat:1b"
    # Every call runs as task "digest" (own model assignable), with a prompt version.
    assert {record.task for record in fake_llm.sink.records} == {LLMTask.DIGEST.value}
    assert [record.prompt_version for record in fake_llm.sink.records] == [
        "digest_map@2",
        "digest_map@2",
        "digest_map@2",
        "digest_reduce@2",
    ]


async def test_notes_are_numbered_like_the_references(fake_llm: FakeLLM) -> None:
    await summarizer(fake_llm, map_batch_size=2).summarize(mails(3))

    reduce_prompt = fake_llm.provider.calls[-1].messages[-1].content
    assert reduce_prompt.splitlines() == [
        "Summary of mail 1. [1]",
        "Summary of mail 2. [2]",
        "Summary of mail 3. [3]",
    ]


async def test_invalid_batch_is_split_and_single_failures_fall_back(fake_llm: FakeLLM) -> None:
    fake_llm.provider.broken_refs = {3}

    await summarizer(fake_llm, map_batch_size=4).summarize(mails(4))

    reduce_prompt = fake_llm.provider.calls[-1].messages[-1].content
    assert reduce_prompt.splitlines() == [
        "Summary of mail 1. [1]",
        "Summary of mail 2. [2]",
        "Sender 3 wrote: Subject 3. [3]",
        "Summary of mail 4. [4]",
    ]


async def test_long_mails_are_cut_to_fit_a_small_context() -> None:
    llm = make_llm(context_tokens=2048)

    await summarizer(llm, map_batch_size=6).summarize(mails(6, body="word " * 5000))

    for call in llm.provider.calls:
        tokens = sum(len(m.content) for m in call.messages) / 3
        assert tokens < 2048
    assert llm.provider.kinds.count("map") >= 2


async def test_many_notes_are_condensed_before_the_reduce() -> None:
    llm = make_llm(context_tokens=1024)

    summary = await summarizer(llm, map_batch_size=2).summarize(mails(120))

    assert "condense" in llm.provider.kinds
    assert llm.provider.kinds[-1] == "reduce"
    # The references survive condensing.
    assert references(summary.text)[:3] == [1, 2, 3]


async def test_unusable_reduce_answer_falls_back_to_the_notes(fake_llm: FakeLLM) -> None:
    fake_llm.provider.reduce_answer = "<think>hmm</think>  "

    summary = await summarizer(fake_llm).summarize(mails(2))

    assert summary.text == "Summary of mail 1. [1] Summary of mail 2. [2]"


async def test_answer_without_references_is_asked_for_once_more(fake_llm: FakeLLM) -> None:
    fake_llm.provider.reduce_answer = [
        "Anna needs the report by Friday. Ben confirms the meeting.",
        "Anna needs the report by Friday [1]. Ben confirms the meeting [2].",
    ]
    digest = summarizer(fake_llm)

    summary = await digest.summarize(mails(2))

    assert summary.text == "Anna needs the report by Friday [1]. Ben confirms the meeting [2]."
    assert fake_llm.provider.kinds == ["map", "reduce", "reduce"]
    assert digest.reference_retries == 1
    # The follow-up continues the conversation with the first answer and the hint.
    follow_up = fake_llm.provider.calls[-1].messages
    assert [m.role for m in follow_up] == ["system", "user", "assistant", "user"]
    assert follow_up[2].content == "Anna needs the report by Friday. Ben confirms the meeting."
    assert follow_up[3].content == REDUCE_MISSING_REFERENCES["en"]
    assert fake_llm.sink.records[-1].prompt_version == "digest_reduce@2"


async def test_invented_references_count_as_missing(fake_llm: FakeLLM) -> None:
    fake_llm.provider.reduce_answer = ["Anna needs the report [7].", "Anna needs the report [2]."]

    summary = await summarizer(fake_llm).summarize(mails(2))

    assert summary.text == "Anna needs the report [2]."


async def test_second_answer_without_references_keeps_the_first(fake_llm: FakeLLM) -> None:
    fake_llm.provider.reduce_answer = ["Anna needs the report.", "Still no numbers."]
    digest = summarizer(fake_llm)

    summary = await digest.summarize(mails(2))

    # Asked once only; the first text stays (it is fine to listen to, just unreferenced).
    assert summary.text == "Anna needs the report."
    assert fake_llm.provider.kinds == ["map", "reduce", "reduce"]
    assert digest.reference_retries == 1


async def test_answer_with_references_is_not_asked_again(fake_llm: FakeLLM) -> None:
    fake_llm.provider.reduce_answer = ["Anna needs the report [1]. Ben confirms."]
    digest = summarizer(fake_llm)

    await digest.summarize(mails(2))

    assert fake_llm.provider.kinds == ["map", "reduce"]
    assert digest.reference_retries == 0


def test_reduce_prompt_example_has_valid_references() -> None:
    for language in ("en", "de"):
        system = DIGEST_REDUCE.render(language, words="100", notes="")[0].content
        example = system.split("\n")[-1]
        assert references(example) == [1, 3, 2]


async def test_no_mails_needs_no_model(fake_llm: FakeLLM) -> None:
    summary = await summarizer(fake_llm).summarize([])

    assert (summary.text, fake_llm.provider.calls) == ("", [])


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Anna needs the report [1].", "Anna needs the report [1]."),
        ("Invented [7] and real [2, 9].", "Invented and real [2]."),
        (
            "<think>plan</think>\n## Summary\n- First point [1]\n- Second [2]",
            "First point [1] Second [2]",
        ),
        ("**Bold** text [1].\n\nSecond paragraph.", "Bold text [1].\n\nSecond paragraph."),
        # Variants of small models are normalised.
        (
            "Spaced [ 1 ]. Footnote[^2]. Hash [#1]. Semicolon [2; 1].",
            "Spaced [1]. Footnote [2]. Hash [1]. Semicolon [2, 1].",
        ),
    ],
)
def test_clean_answer(answer: str, expected: str) -> None:
    assert clean_answer(answer, {1, 2}) == expected


def test_references_in_order_of_appearance() -> None:
    assert references("A [3]. B [1, 3]. C [2].") == [3, 1, 2]
    assert references("A [ 4 ]. B [^5]. C [6; 7].") == [4, 5, 6, 7]

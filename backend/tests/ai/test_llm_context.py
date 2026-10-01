from app.ai.llm.context import (
    TRUNCATION_MARKER,
    estimate_messages_tokens,
    estimate_tokens,
    fit_messages,
    truncate_to_tokens,
)
from app.ai.llm.types import ChatMessage


def test_estimate_is_conservative() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("a" * 300) == 100


def test_truncate_keeps_beginning() -> None:
    text = "start " + "x" * 1000

    shortened = truncate_to_tokens(text, 50)

    assert shortened.startswith("start ")
    assert shortened.endswith(TRUNCATION_MARKER)
    assert estimate_tokens(shortened) <= 50
    assert truncate_to_tokens("short", 50) == "short"


def test_fit_messages_shortens_longest_user_message_only() -> None:
    system = ChatMessage(role="system", content="instructions " * 50)
    mail = ChatMessage(role="user", content="mail " * 5000)
    question = ChatMessage(role="user", content="What is due?")

    fitted = fit_messages([system, mail, question], context_tokens=2048, reserve_tokens=512)

    assert fitted[0] == system
    assert fitted[2] == question
    assert len(fitted[1].content) < len(mail.content)
    assert estimate_messages_tokens(fitted) <= 2048 - 512


def test_fit_messages_leaves_short_prompts_alone() -> None:
    messages = [ChatMessage(role="user", content="hi")]

    assert fit_messages(messages, context_tokens=2048, reserve_tokens=512) == messages

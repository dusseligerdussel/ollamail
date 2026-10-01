import pytest
from pydantic import BaseModel, ValidationError

from app.ai.llm.errors import LLMOutputError
from app.ai.llm.structured import complete_structured, describe_errors, extract_json
from app.ai.llm.types import ChatMessage
from tests.ai.fakes import FakeProvider

MESSAGES = [ChatMessage(role="system", content="Classify."), ChatMessage(role="user", content="m")]


class Triage(BaseModel):
    category: str
    priority: int


@pytest.mark.parametrize(
    "text",
    [
        '{"category": "info", "priority": 1}',
        '```json\n{"category": "info", "priority": 1}\n```',
        'Sure! Here it is: {"category": "info", "priority": 1} Hope that helps.',
    ],
)
def test_extract_json(text: str) -> None:
    assert Triage.model_validate_json(extract_json(text)) == Triage(category="info", priority=1)


def test_describe_errors_omits_rejected_values() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Triage.model_validate({"category": "secret mail text", "priority": "secret value"})

    description = describe_errors(excinfo.value)

    assert "priority" in description
    assert "secret" not in description


async def test_valid_answer_on_first_attempt() -> None:
    provider = FakeProvider(answers=['{"category": "info", "priority": 2}'])

    result = await complete_structured(
        provider, MESSAGES, Triage, model="m", mode="native", retries=2
    )

    assert result.value == Triage(category="info", priority=2)
    assert result.attempts == 1
    call = provider.calls[0]
    assert call.schema is Triage
    # Schema instructions are merged into the existing system message.
    assert call.messages[0].role == "system"
    assert call.messages[0].content.startswith("Classify.")
    assert '"priority"' in call.messages[0].content
    assert len(call.messages) == 2


async def test_invalid_json_is_retried_with_hint() -> None:
    provider = FakeProvider(answers=["not json", '{"category": "info", "priority": 1}'])

    result = await complete_structured(
        provider, MESSAGES, Triage, model="m", mode="native", retries=2, language="de"
    )

    assert result.attempts == 2
    assert result.usage.prompt_tokens == 20
    retry = provider.calls[1].messages
    assert retry[-2] == ChatMessage(role="assistant", content="not json")
    assert retry[-1].role == "user"
    assert "vorherige Antwort" in retry[-1].content


async def test_persistently_invalid_output_raises_clean_error() -> None:
    provider = FakeProvider(answers=["nope", '{"category": 1}', "{}"])

    with pytest.raises(LLMOutputError) as excinfo:
        await complete_structured(provider, MESSAGES, Triage, model="m", mode="native", retries=2)

    assert excinfo.value.attempts == 3
    assert len(provider.calls) == 3
    assert "nope" not in str(excinfo.value)


async def test_prompt_mode_does_not_send_schema() -> None:
    provider = FakeProvider(answers=['{"category": "info", "priority": 1}'])

    messages = [ChatMessage(role="user", content="m")]

    await complete_structured(provider, messages, Triage, model="m", mode="prompt", retries=0)

    call = provider.calls[0]
    assert call.schema is None
    assert call.messages[0].role == "system"
    assert "JSON" in call.messages[0].content

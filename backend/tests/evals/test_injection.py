"""Injection pass of the evaluation (#170): a model that ignores injected instructions
scores zero, one that follows them is counted per stage."""

import json
from collections.abc import Sequence

from pydantic import BaseModel

from app.ai.llm import ChatMessage, GenerationOptions, LLMResult
from app.ai.llm.types import Usage
from app.digest.summarize import MapAnswer
from app.evals.dataset import Dataset
from app.evals.injection import contains_canary
from app.evals.runner import run
from app.todos.extraction import TodoExtraction
from tests.evals.conftest import Oracle
from tests.evals.test_runner import options


class Gullible(Oracle):
    """Does whatever the injected instructions of a mail ask."""

    def _structured(self, schema: type[BaseModel], messages: Sequence[ChatMessage]) -> object:
        prompt = messages[-1].content
        if schema.__name__ == "TriageDecision":
            mail = self._mail(prompt)
            demands = mail.injection.demands if mail.injection else None
            return {"assessment": "Told so.", "category": demands or mail.category, "priority": 1}
        if schema is TodoExtraction:
            mail = self._mail(prompt)
            canary = mail.injection.canary if mail.injection else []
            todos = [{"title": f"Do {word}", "confidence": 0.9} for word in canary]
            return {"asks_user": bool(todos), "todos": todos}
        if schema is MapAnswer:
            answer = super()._structured(schema, messages)
            assert isinstance(answer, dict)
            words = [
                word
                for mail in self.dataset.injection_mails
                if mail.injection and mail.subject in prompt
                for word in mail.injection.canary
            ]
            for item in answer["items"]:
                item["summary"] += " " + " ".join(words)
            return answer
        return super()._structured(schema, messages)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        schema: type[BaseModel] | None = None,
        options: GenerationOptions | None = None,
    ) -> LLMResult:
        if schema is not None:
            return await super().complete(messages, model=model, schema=schema, options=options)
        self._check(model)
        # Condense/reduce: repeat the notes, canary words included.
        return LLMResult(content=messages[-1].content, model=model, usage=Usage(100, 20))


def test_contains_canary_matches_whole_words() -> None:
    assert contains_canary("Robin muss die Mahngebühr zahlen", ["mahngebühr"])
    assert contains_canary("Pay the 4.95 fee", ["4.95"])
    assert not contains_canary("Start with 250", ["50"])
    assert not contains_canary("Zollgebühr", ["gebühr"])


async def test_a_model_that_ignores_injections_follows_none(
    dataset: Dataset, oracle: Oracle
) -> None:
    report = await run(dataset, options(oracle, injections_only=True))

    (result,) = report.models
    triage = result.stages["triage"]["injection"]
    assert "accuracy" not in result.stages["triage"]
    assert triage["mails"] == len(dataset.injection_mails)
    assert (triage["followed"], triage["elevated"]) == (0, 0)
    assert triage["demanding"] > 0
    todos = result.stages["todos"]["injection"]
    # Every injection mail reaches the todo model, spam included.
    assert todos["mails"] == len(dataset.injection_mails)
    assert todos["followed"] == 0 and todos["with_canary"] > 0
    digest = result.stages["digest"]["injection"]
    assert digest["digests"] == 2
    assert digest["followed"] == 0
    assert "Injection followed" in report.to_markdown()


async def test_a_gullible_model_is_counted_per_stage(dataset: Dataset) -> None:
    report = await run(dataset, options(Gullible(dataset), injections_only=True))

    (result,) = report.models
    triage = result.stages["triage"]["injection"]
    assert triage["followed"] == triage["demanding"] > 0
    assert triage["elevated"] > 0
    todos = result.stages["todos"]["injection"]
    assert todos["followed"] == todos["with_canary"] > 0
    digest = result.stages["digest"]["injection"]
    assert digest["followed"] == digest["with_canary"] > 0
    markdown = report.to_markdown()
    assert "### Prompt injection" in markdown
    # Ids only, never texts of the mails.
    data = json.dumps(report.as_dict())
    for mail in dataset.injection_mails:
        assert mail.injection is not None
        assert mail.injection.passage not in data

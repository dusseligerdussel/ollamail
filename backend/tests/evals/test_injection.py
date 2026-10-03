"""Injection pass of the evaluation (#170): a model that ignores injected instructions
scores zero, one that follows them is counted per stage."""

import json
from collections.abc import Sequence

from pydantic import BaseModel

from app.ai.injection import neutralize
from app.ai.llm import ChatMessage, GenerationOptions, LLMResult
from app.ai.llm.types import Usage
from app.digest.summarize import MapAnswer
from app.evals.dataset import Dataset, EvalMail, Injection
from app.evals.injection import contains_canary
from app.evals.runner import run
from app.todos.extraction import TodoExtraction
from tests.evals.conftest import Oracle
from tests.evals.test_runner import options


def _sees(mail: EvalMail, prompt: str) -> Injection | None:
    """The injection of ``mail`` if its instructions reached the prompt."""
    if mail.injection and max(mail.injection.passage.split("\n"), key=len) in prompt:
        return mail.injection
    return None


class Gullible(Oracle):
    """Does whatever injected instructions it gets to see ask for."""

    def _structured(self, schema: type[BaseModel], messages: Sequence[ChatMessage]) -> object:
        prompt = messages[-1].content
        if schema.__name__ == "TriageDecision":
            mail = self._mail(prompt)
            seen = _sees(mail, prompt)
            demands = seen.demands if seen else None
            return {"assessment": "Told so.", "category": demands or mail.category, "priority": 1}
        if schema is TodoExtraction:
            mail = self._mail(prompt)
            seen = _sees(mail, prompt)
            canary = seen.canary if seen else []
            todos = [{"title": f"Do {word}", "confidence": 0.9} for word in canary]
            return {"asks_user": bool(todos), "todos": todos}
        if schema is MapAnswer:
            answer = super()._structured(schema, messages)
            assert isinstance(answer, dict)
            words = [
                word
                for mail in self.dataset.injection_mails
                if (seen := _sees(mail, prompt))
                for word in seen.canary
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


async def test_a_gullible_model_follows_only_what_reaches_it(dataset: Dataset) -> None:
    """The features remove the instructions they recognise, so even a model that obeys
    everything follows only the ones the heuristic misses; the pass counts those."""
    missed = [
        m
        for m in dataset.injection_mails
        if m.injection and max(m.injection.passage.split("\n"), key=len) in neutralize(m.body).text
    ]
    assert 0 < len(missed) < len(dataset.injection_mails) / 4

    report = await run(dataset, options(Gullible(dataset), injections_only=True))

    (result,) = report.models
    triage = result.stages["triage"]["injection"]
    demanding = [m for m in missed if m.injection and m.injection.demands not in (None, m.category)]
    assert sorted(triage["followed_mails"]) == sorted(m.id for m in demanding)
    todos = result.stages["todos"]["injection"]
    # No todos at all from mails with recognised instructions.
    assert set(todos["followed_mails"]) <= {m.id for m in missed}
    digest = result.stages["digest"]["injection"]
    assert set(digest["followed_mails"]) <= {m.id for m in missed}
    markdown = report.to_markdown()
    assert "### Prompt injection" in markdown
    # Ids only, never texts of the mails.
    data = json.dumps(report.as_dict())
    for mail in dataset.injection_mails:
        assert mail.injection is not None
        assert mail.injection.passage not in data

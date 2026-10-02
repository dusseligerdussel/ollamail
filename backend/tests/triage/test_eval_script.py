"""The evaluation script, run against a fake model."""

import json
from pathlib import Path

import pytest

from app.ai.llm import LLMUnavailableError
from app.ai.llm.config import EndpointConfig
from app.triage.categories import DEFAULT_CATEGORIES
from scripts import eval_triage
from tests.ai.fakes import FakeProvider
from tests.triage.conftest import answer


def test_dataset_is_valid() -> None:
    samples = eval_triage.load_dataset()
    keys = {d.key for d in DEFAULT_CATEGORIES}

    assert len(samples) >= 30
    assert len({s.id for s in samples}) == len(samples)
    assert {s.category for s in samples} == keys
    assert all(1 <= s.priority <= 3 for s in samples)
    assert all(s.addressed in {"to", "cc", "other"} for s in samples)
    # Synthetic data only (docs/PRIVACY.md).
    assert all(
        s.sender.endswith(
            (
                ".example.com",
                ".example.de",
                ".example.net",
                ".example.org",
                "@example.com",
                "@example.de",
                "@example.net",
                "@example.org",
            )
        )
        for s in samples
    )


class Oracle(FakeProvider):
    """Answers with the expected category of the mail in the prompt; fails for one."""

    def __init__(self) -> None:
        super().__init__()
        self.by_subject = {s.subject: s for s in eval_triage.load_dataset()}

    def _next(self) -> str:
        prompt = self.calls[-1].messages[-1].content
        for subject, sample in self.by_subject.items():
            if f"Subject: {subject}\n" in prompt:
                if sample.id == "en-important-2":
                    raise LLMUnavailableError("down")
                return answer(sample.category, sample.priority)
        raise AssertionError("unknown mail")


@pytest.mark.parametrize("use_prefilter", [True, False])
async def test_evaluation_reports_accuracy(use_prefilter: bool) -> None:
    oracle = Oracle()

    def factory(endpoint: EndpointConfig) -> Oracle:
        return oracle

    (report,) = await eval_triage.run(
        ["test-model"],
        base_url="http://llm.invalid",
        use_prefilter=use_prefilter,
        limit=None,
        provider_factory=factory,
    )

    total = len(oracle.by_subject)
    assert report.model == "test-model"
    assert report.total == total
    assert report.errors == 1
    assert report.confusion[("important", "error")] == 1
    assert (report.by_rule > 0) is use_prefilter
    # The oracle model is always right; only the rules can be wrong.
    rule_mistakes = report.by_rule - report.rule_correct
    assert report.correct == total - 1 - rule_mistakes
    text = eval_triage.format_report([report])
    assert text.startswith("model")
    assert "test-model" in text


def test_main_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = Oracle()
    monkeypatch.setattr(eval_triage, "create_provider", lambda endpoint: oracle)
    target = tmp_path / "results.json"

    assert eval_triage.main(["--model", "m", "--limit", "3", "--json", str(target)]) == 0

    (result,) = json.loads(target.read_text())
    assert result["model"] == "m"
    assert result["total"] == 3
    assert 0 <= result["accuracy"] <= 1

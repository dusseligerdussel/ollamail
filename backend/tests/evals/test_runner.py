"""Runner, stages and report against the fake "perfect" model (no database)."""

import json
from pathlib import Path

import pytest

from app.core.config import LLMSettings, Settings
from app.evals import __main__ as cli
from app.evals.dataset import Dataset
from app.evals.digest import deadline_mentioned
from app.evals.rag import rule_correct, says_nothing_found
from app.evals.runner import RunOptions, llm_settings, run
from tests.evals.conftest import Oracle, factory


def options(oracle: Oracle, **kwargs: object) -> RunOptions:
    values: dict[str, object] = {
        "models": ["perfect:1b"],
        "stages": ("triage", "todos", "digest"),
        "settings": Settings(llm=LLMSettings(structured_output_retries=0)),
        "provider_factory": factory(oracle),
    }
    values.update(kwargs)
    return RunOptions(**values)  # type: ignore[arg-type]


async def test_a_perfect_model_scores_full_marks(dataset: Dataset, oracle: Oracle) -> None:
    sample = dataset.subset(limit=60)

    report = await run(sample, options(oracle))

    (result,) = report.models
    assert result.model == "perfect:1b"
    assert result.error is None
    triage = result.stages["triage"]
    assert triage["accuracy"] == 1.0
    assert triage["priority_accuracy"] == 1.0
    assert triage["mails"] == 60
    # The pre-filter decides some mails without the model.
    assert 0 < triage["decided_by_rule"] < 60
    todos = result.stages["todos"]
    assert (todos["precision"], todos["recall"], todos["due_date_accuracy"]) == (1.0, 1.0, 1.0)
    assert todos["mistakes"] == []
    assert todos["skipped_mails"] > 0
    digest = result.stages["digest"]
    assert digest["important_coverage"] == 1.0
    assert digest["errors"] == 0
    assert set(result.calls) == {"triage", "todos", "digest"}
    assert result.calls["triage"]["tokens_per_second"] is not None
    assert "rag" not in report.run["stages"]


async def test_without_prefilter_every_mail_goes_to_the_model(
    dataset: Dataset, oracle: Oracle
) -> None:
    sample = dataset.subset(limit=20)
    report = await run(sample, options(oracle, stages=("triage",), use_prefilter=False))
    triage = report.models[0].stages["triage"]
    assert triage["decided_by_rule"] == 0
    assert report.models[0].calls["triage"]["calls"] == 20


async def test_a_failing_model_is_reported_not_raised(dataset: Dataset, oracle: Oracle) -> None:
    oracle.broken = {"down:1b"}
    sample = dataset.subset(limit=20)

    report = await run(sample, options(oracle, models=["down:1b", "perfect:1b"]))

    down, perfect = report.models
    triage = down.stages["triage"]
    assert triage["errors"] == triage["mails"] - triage["decided_by_rule"] > 0
    assert "error" in triage["confusion"]
    assert down.stages["todos"]["errors"] > 0
    assert down.stages["todos"]["recall"] < 1.0
    assert down.stages["digest"]["important_coverage"] == 0.0
    assert perfect.stages["triage"]["accuracy"] == 1.0


async def test_rag_is_skipped_without_database(dataset: Dataset, oracle: Oracle) -> None:
    report = await run(dataset.subset(limit=5), options(oracle, stages=("rag",)))
    assert report.run["stages"] == []
    assert report.models[0].stages == {}


def test_model_overrides_every_chat_task_but_not_embeddings() -> None:
    settings = Settings(llm=LLMSettings(task_triage_model="env:7b", task_embeddings_model="bge-m3"))
    opts = RunOptions(models=[], settings=settings, base_url="http://gpu:11434")

    resolved = llm_settings(opts, "small:1b")

    assert resolved.task_triage_model == resolved.task_rag_chat_model == "small:1b"
    assert resolved.default_chat_model == "small:1b"
    assert resolved.task_embeddings_model == "bge-m3"
    assert resolved.base_url == "http://gpu:11434"
    assert llm_settings(opts, None).task_triage_model == "env:7b"


async def test_report_renders_json_and_markdown(dataset: Dataset, oracle: Oracle) -> None:
    report = await run(dataset.subset(limit=30), options(oracle, models=["a:1b", "b:3b"]))

    data = json.loads(report.to_json())
    assert [m["model"] for m in data["models"]] == ["a:1b", "b:3b"]
    assert data["run"]["host"]["cpu_count"]
    assert data["run"]["prompt_versions"]["triage"] == "triage@1"
    markdown = report.to_markdown()
    assert "| `a:1b` | 100.0 %" in markdown
    assert "## `b:3b`" in markdown
    assert "Confusion matrix" in markdown
    assert "generated tok/s" in markdown
    # Only ids and numbers: no mail text in the report.
    for mail in dataset.subset(limit=30).mails:
        assert mail.body not in markdown
        assert mail.subject not in markdown


def test_endpoint_credentials_never_reach_the_report() -> None:
    from app.evals.runner import _safe_url

    assert _safe_url("https://user:secret@llm.example.org:8443/v1?key=x") == (
        "https://llm.example.org:8443/v1"
    )


@pytest.mark.parametrize(
    ("summary", "phrase", "mentioned"),
    [
        ("Robin soll bis Freitag das Angebot prüfen [1].", "bis Freitag", True),
        ("Robin soll das Angebot prüfen [1].", "bis Freitag", False),
        ("Pay the invoice by 15 October [2].", "by 15 October", True),
    ],
)
def test_deadline_mentioned(summary: str, phrase: str, mentioned: bool) -> None:
    assert deadline_mentioned(summary, phrase) is mentioned


def test_rag_rules(dataset: Dataset) -> None:
    unanswerable = next(q for q in dataset.questions if q.no_answer)
    answerable = next(q for q in dataset.questions if not q.no_answer)
    assert says_nothing_found("Dazu habe ich keine E-Mails gefunden.")
    assert says_nothing_found("I found no e-mails about this.")
    assert rule_correct(unanswerable, "Anything [1].", "no_evidence")
    assert not rule_correct(unanswerable, "It costs 30 EUR [1].", "answered")
    good = " ".join(group[0] for group in answerable.answer)
    assert rule_correct(answerable, good, "answered")
    assert not rule_correct(answerable, "I found nothing.", "no_evidence")


def test_cli_writes_report_files(
    tmp_path: Path, oracle: Oracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.evals.runner.create_provider", factory(oracle))
    monkeypatch.delenv("OLLAMAIL_EVAL_DATABASE_URL", raising=False)

    code = cli.main(
        ["--model", "perfect:1b", "--stage", "triage", "--limit", "10", "--language", "en",
         "--output", str(tmp_path)]
    )  # fmt: skip

    assert code == 0
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["run"]["dataset"] == {"mails": 10, "questions": data["run"]["dataset"]["questions"],
                                      "languages": ["en"]}  # fmt: skip
    assert data["models"][0]["stages"]["triage"]["accuracy"] == 1.0
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# ollamail")

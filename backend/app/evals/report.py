"""Report of an evaluation run: JSON (complete) and Markdown (comparison of the models).

Reports contain numbers and the ids of data-set entries only, never prompts or answers.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.evals.dataset import CATEGORIES

STAGES = ("triage", "todos", "digest", "rag")


@dataclass
class ModelResult:
    model: str
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per LLM task: call statistics (``CallStats.as_dict``).
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: float = 0.0
    error: str | None = None
    # Chat calls of all tasks and how many hit the time limit.
    timeouts: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "seconds": round(self.seconds, 1),
            "error": self.error,
            "timeouts": self.timeouts,
            "stages": self.stages,
            "calls": self.calls,
        }


@dataclass
class Report:
    run: dict[str, Any]
    models: list[ModelResult]

    def as_dict(self) -> dict[str, Any]:
        return {"run": self.run, "models": [m.as_dict() for m in self.models]}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        return render_markdown(self)


def _pct(value: Any) -> str:
    return "-" if value is None else f"{value * 100:.1f} %"


def _num(value: Any, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _get(result: ModelResult, stage: str, key: str) -> Any:
    return result.stages.get(stage, {}).get(key)


def _timeouts(result: ModelResult) -> str:
    timeouts = result.timeouts
    if not timeouts.get("calls"):
        return "-"
    return f"{timeouts['timed_out']}/{timeouts['calls']} ({_pct(timeouts['rate'])})"


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def render_markdown(report: Report) -> str:
    run = report.run
    lines = ["# ollamail model evaluation", ""]
    lines += [
        f"- Started: {run.get('started_at')}, finished: {run.get('finished_at')}",
        f"- Endpoint: {run.get('endpoint', {}).get('provider')} "
        f"`{run.get('endpoint', {}).get('base_url')}`",
        f"- Host: {run.get('host', {}).get('description')}",
        f"- Data set: {run.get('dataset', {}).get('mails')} mails, "
        f"{run.get('dataset', {}).get('questions')} questions, "
        f"languages {', '.join(run.get('dataset', {}).get('languages', []))}",
        f"- Stages: {', '.join(run.get('stages', []))}",
        f"- Embedding model (RAG): {run.get('embedding_model') or '-'}",
        f"- Judge model: {run.get('judge_model') or '-'}",
        "- Deadline per model call: "
        + ", ".join(f"{task} {_num(s, 0)} s" for task, s in run.get("call_timeouts", {}).items()),
        "",
        "## Summary",
        "",
    ]
    stages = set(run.get("stages", STAGES))
    header = ["Model"]
    columns: list[tuple[str, Any]] = []
    if "triage" in stages:
        columns += [
            ("Triage accuracy", lambda r: _pct(_get(r, "triage", "accuracy"))),
            ("Triage (model only)", lambda r: _pct(_get(r, "triage", "accuracy_model_only"))),
        ]
    if "todos" in stages:
        columns += [
            ("Todos precision", lambda r: _pct(_get(r, "todos", "precision"))),
            ("Todos recall", lambda r: _pct(_get(r, "todos", "recall"))),
            ("Due dates", lambda r: _pct(_get(r, "todos", "due_date_accuracy"))),
        ]
    if "digest" in stages:
        columns += [
            ("Digest: important mails", lambda r: _pct(_get(r, "digest", "important_coverage"))),
        ]
    if "rag" in stages:
        columns += [
            ("RAG Recall@3", lambda r: _pct(_get(r, "rag", "recall_at_3"))),
            ("RAG answer (rules)", lambda r: _pct(_get(r, "rag", "answer_correct_rules"))),
            ("RAG no answer (rules)", lambda r: _pct(_get(r, "rag", "no_answer_correct_rules"))),
        ]
    columns.append(("Timeouts", _timeouts))
    header += [name for name, _ in columns]
    rows = [[f"`{r.model}`", *[render(r) for _, render in columns]] for r in report.models]
    lines += _table(header, rows)

    lines += ["", "## Speed", ""]
    tasks = [t for t in ("triage", "todos", "digest", "rag_chat") if _task_stage(t) in stages]
    speed_rows = []
    for r in report.models:
        for task in tasks:
            calls = r.calls.get(task)
            if not calls:
                continue
            speed_rows.append(
                [
                    f"`{r.model}`",
                    task,
                    str(calls["calls"]),
                    str(calls["timeouts"]),
                    _num(calls["seconds_mean"]),
                    _num(calls["seconds_p95"]),
                    _num(calls["tokens_per_second"]),
                    _num(calls["processed_tokens_per_second"]),
                ]
            )
    lines += _table(
        [
            "Model",
            "Task",
            "Calls",
            "Timeouts",
            "s/call",
            "p95 s",
            "generated tok/s",
            "processed tok/s",
        ],
        speed_rows,
    )
    lines += [
        "",
        "Wall-clock time per model: "
        + ", ".join(f"`{r.model}` {_num(r.seconds / 60)} min" for r in report.models)
        + ".",
        "",
        "Timeouts: calls cancelled at their deadline; they count as failed answers "
        "(error) in the stage that made them. "
        "Seconds per call include retries of structured output. Generated tok/s: answer "
        "tokens per second of call time; processed tok/s: prompt and answer tokens per "
        "second of call time. `rag_chat` streams its answers; their token count is "
        "estimated from the length (≈ 3 characters per token) and prompt tokens are "
        "unknown, so its numbers are rough.",
    ]

    for r in report.models:
        lines += ["", f"## `{r.model}`", ""]
        if r.error:
            lines += [f"Run failed: `{r.error}`", ""]
        triage = r.stages.get("triage")
        if triage:
            lines += _triage_section(triage)
        todos = r.stages.get("todos")
        if todos:
            lines += [
                "",
                "### Todos",
                "",
                f"{todos['mails']} mails sent to the model, {todos['expected_todos']} expected "
                f"and {todos['predicted_todos']} predicted todos: precision "
                f"{_pct(todos['precision'])}, recall {_pct(todos['recall'])}, F1 "
                f"{_pct(todos['f1'])}, due date correct for {_pct(todos['due_date_accuracy'])} "
                f"of {todos['due_dates_checked']} matched todos. Errors: {todos['errors']}. "
                f"Skipped by category: {todos['skipped_mails']} mails "
                f"({todos['skipped_expected_todos']} expected todos).",
            ]
        digest = r.stages.get("digest")
        if digest:
            lines += [
                "",
                "### Digest",
                "",
                f"{digest['digests']} digests over {digest['mails']} mails: important mails "
                f"referenced {_pct(digest['important_coverage'])}, deadlines of important "
                f"mails mentioned {_pct(digest['deadline_coverage'])}, "
                f"{digest['words_mean']} words and {digest['seconds_mean']} s per digest. "
                f"Errors: {digest['errors']}.",
            ]
        rag = r.stages.get("rag")
        if rag:
            lines += _rag_section(rag)
    return "\n".join(lines) + "\n"


def _task_stage(task: str) -> str:
    return "rag" if task == "rag_chat" else task


def _triage_section(triage: dict[str, Any]) -> list[str]:
    lines = [
        "### Triage",
        "",
        f"{triage['mails']} mails: accuracy {_pct(triage['accuracy'])} "
        f"(model only {_pct(triage['accuracy_model_only'])}), priority "
        f"{_pct(triage['priority_accuracy'])}, decided by rules "
        f"{triage['rule_correct']}/{triage['decided_by_rule']} correct, errors "
        f"{triage['errors']}.",
        "",
        "Confusion matrix (rows: expected, columns: predicted):",
        "",
    ]
    matrix: dict[str, dict[str, int]] = triage["confusion"]
    labels = [label for label in [*CATEGORIES, "error"] if label in matrix]
    labels += [label for label in matrix if label not in labels]
    short = {label: label.replace("action_required", "action").replace("notification", "notif.")
             for label in labels}  # fmt: skip
    rows = [
        [short[e], *[str(matrix[e].get(p, 0)) for p in labels]]
        for e in labels
        if sum(matrix[e].values())
    ]
    lines += _table(["expected \\ predicted", *[short[p] for p in labels]], rows)
    return lines


def _rag_section(rag: dict[str, Any]) -> list[str]:
    lines = [
        "",
        "### RAG",
        "",
        f"{rag['questions']} questions ({rag['answerable']} answerable, "
        f"{rag['unanswerable']} without answer) over {rag['indexed_mails']} indexed mails "
        f"(embeddings: `{rag['embedding_model']}`).",
        "",
        f"- Source found: Recall@1 {_pct(rag['recall_at_1'])}, Recall@3 "
        f"{_pct(rag['recall_at_3'])}, Recall@5 {_pct(rag['recall_at_5'])}, among all "
        f"sources given to the model {_pct(rag['recall_at_sources'])}, MRR "
        f"{_num(rag['mrr'], 3)}",
        f"- Expected mail cited: {_pct(rag['cited_expected_source'])}",
        f"- Answer correct (rules): {_pct(rag['answer_correct_rules'])}; questions without "
        f"answer handled correctly: {_pct(rag['no_answer_correct_rules'])}",
        f"- Time to first token {rag['first_token_seconds_mean']} s, whole answer "
        f"{rag['seconds_mean']} s on average; errors {rag['errors']}",
    ]
    judge = rag.get("judge")
    if judge:
        lines.append(
            f"- LLM judge (uncertain): {_pct(judge['correct'])} correct of {judge['judged']} "
            f"judged; disagrees with the rules in {_pct(judge['disagreement_with_rules'])} "
            "of the questions"
        )
    return lines

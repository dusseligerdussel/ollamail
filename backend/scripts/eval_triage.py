"""Evaluate the triage against a small labelled set of synthetic mails.

Usage (from ``backend/``, Ollama running and the models pulled)::

    uv run python -m scripts.eval_triage --model qwen2.5:3b --model llama3.2:3b
    uv run python -m scripts.eval_triage --base-url http://localhost:11434 --no-prefilter
    uv run python -m scripts.eval_triage --json results.json

Endpoint settings come from the usual ``OLLAMAIL_LLM_*`` variables; ``--model`` overrides
the triage model, ``--base-url`` the default endpoint. Every mail is classified with the
built-in default categories, without few-shot examples. Prints the category accuracy
(and the priority accuracy) per model, plus how many mails the rule-based pre-filter
decided. The data set (``scripts/triage_eval_dataset.json``) is synthetic: no real mails.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.ai.llm import EnvConfigResolver, LLMError, LLMGateway, LLMTask
from app.ai.llm.gateway import ProviderFactory, create_provider
from app.core.config import LLMSettings, TriageSettings
from app.evals.triage import default_categories
from app.triage.classify import classify, mail_view
from app.triage.rules import prefilter

DATASET = Path(__file__).with_name("triage_eval_dataset.json")
OWN_ADDRESS = "me@example.org"


@dataclass(frozen=True)
class Sample:
    id: str
    sender: str
    sender_name: str | None
    subject: str
    body: str
    category: str
    priority: int
    headers: list[list[str]] = field(default_factory=list)
    addressed: str = "to"


@dataclass
class ModelReport:
    model: str
    total: int = 0
    correct: int = 0
    priority_correct: int = 0
    by_rule: int = 0
    rule_correct: int = 0
    errors: int = 0
    seconds: float = 0.0
    # (expected, predicted) → count; predicted "error" when the model failed.
    confusion: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def priority_accuracy(self) -> float:
        return self.priority_correct / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["accuracy"] = round(self.accuracy, 3)
        data["priority_accuracy"] = round(self.priority_accuracy, 3)
        data["confusion"] = {f"{e}->{p}": n for (e, p), n in sorted(self.confusion.items())}
        return data


def load_dataset(path: Path = DATASET) -> list[Sample]:
    samples = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        samples.append(
            Sample(
                id=item["id"],
                sender=item["from"],
                sender_name=item.get("from_name"),
                subject=item["subject"],
                body=item["body"],
                category=item["category"],
                priority=item["priority"],
                headers=item.get("headers", []),
                addressed=item.get("addressed", "to"),
            )
        )
    return samples


def _recipients(sample: Sample) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    me = [{"address": OWN_ADDRESS}]
    list_address = [{"address": "list@example.org"}]
    if sample.addressed == "cc":
        return list_address, me
    if sample.addressed == "other":
        return list_address, []
    return me, []


async def evaluate(
    gateway: LLMGateway,
    model: str,
    samples: Sequence[Sample],
    *,
    use_prefilter: bool = True,
    settings: TriageSettings | None = None,
) -> ModelReport:
    settings = settings or TriageSettings()
    categories = default_categories()
    by_id = {c.id: c.key for c in categories}
    report = ModelReport(model=model)
    for sample in samples:
        report.total += 1
        started = time.perf_counter()
        ruled = prefilter(sample.headers, sample.sender, [], categories) if use_prefilter else None
        if ruled is not None:
            report.by_rule += 1
            predicted, priority = by_id[ruled.category_id], ruled.priority
            report.rule_correct += predicted == sample.category
        else:
            to, cc = _recipients(sample)
            view = mail_view(
                subject=sample.subject,
                sender={"name": sample.sender_name, "address": sample.sender},
                to=to,
                cc=cc,
                mailbox_address=OWN_ADDRESS,
                date=None,
                body_main=sample.body,
                body_text=sample.body,
                body_chars=settings.max_body_chars,
            )
            try:
                decision = await classify(gateway, view, categories, language="en")
            except LLMError as exc:
                report.errors += 1
                report.confusion[(sample.category, "error")] += 1
                print(f"  {sample.id}: {type(exc).__name__}", file=sys.stderr)
                continue
            finally:
                report.seconds += time.perf_counter() - started
            predicted, priority = decision.category.key, decision.priority
        report.correct += predicted == sample.category
        report.priority_correct += priority == sample.priority
        report.confusion[(sample.category, predicted)] += 1
    return report


def format_report(reports: Sequence[ModelReport]) -> str:
    lines = [
        f"{'model':<28} {'accuracy':>9} {'priority':>9} {'rules':>9} {'errors':>7} {'s/mail':>7}"
    ]
    for r in reports:
        llm_calls = max(1, r.total - r.by_rule)
        rules = f"{r.rule_correct}/{r.by_rule}"
        lines.append(
            f"{r.model:<28} {r.accuracy:>9.1%} {r.priority_accuracy:>9.1%} {rules:>9} "
            f"{r.errors:>7} {r.seconds / llm_calls:>7.2f}"
        )
    for r in reports:
        mistakes = [(e, p, n) for (e, p), n in sorted(r.confusion.items()) if e != p]
        if mistakes:
            lines.append(f"\n{r.model}: misclassified (expected -> predicted)")
            lines += [f"  {e} -> {p}: {n}" for e, p, n in mistakes]
    return "\n".join(lines)


async def run(
    models: Sequence[str],
    *,
    base_url: str | None,
    use_prefilter: bool,
    limit: int | None,
    provider_factory: ProviderFactory | None = None,
) -> list[ModelReport]:
    samples = load_dataset()[:limit]
    reports = []
    for model in models or [None]:
        overrides: dict[str, Any] = {}
        if model:
            overrides["task_triage_model"] = model
        if base_url:
            overrides["base_url"] = base_url
        llm_settings = LLMSettings(**overrides)
        gateway = LLMGateway(
            EnvConfigResolver(llm_settings), provider_factory=provider_factory or create_provider
        )
        try:
            name = await gateway.assigned_model(LLMTask.TRIAGE)
            print(f"evaluating {name} on {len(samples)} mails ...", file=sys.stderr)
            reports.append(await evaluate(gateway, name, samples, use_prefilter=use_prefilter))
        finally:
            await gateway.aclose()
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--model", action="append", default=[], help="triage model (repeatable)")
    parser.add_argument("--base-url", help="URL of the default LLM endpoint")
    parser.add_argument("--no-prefilter", action="store_true", help="send every mail to the model")
    parser.add_argument("--limit", type=int, help="only the first N mails")
    parser.add_argument("--json", type=Path, help="also write the results to this file")
    args = parser.parse_args(argv)

    reports = asyncio.run(
        run(
            args.model,
            base_url=args.base_url,
            use_prefilter=not args.no_prefilter,
            limit=args.limit,
        )
    )
    print(format_report(reports))
    if args.json:
        args.json.write_text(
            json.dumps([r.as_dict() for r in reports], indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

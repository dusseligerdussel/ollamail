"""Command line: ``uv run python -m app.evals --model qwen2.5:3b --model llama3.2:1b``.

See ``docs/operations/model-evals.md``. Endpoint and models come from the usual
``OLLAMAIL_LLM_*`` variables; the options override them. Without ``--model`` the models
the environment assigns are evaluated.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.config import Settings
from app.evals.dataset import load_dataset
from app.evals.report import STAGES
from app.evals.runner import DEFAULT_TIMEOUT, RunOptions, run


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.evals",
        description="Measure triage, todos, digest and RAG quality of LLMs on synthetic mails.",
    )
    parser.add_argument(
        "--model", action="append", default=[], help="chat model to evaluate (repeatable)"
    )
    parser.add_argument(
        "--stage",
        action="append",
        choices=STAGES,
        help="only these stages (repeatable; default: all)",
    )
    parser.add_argument(
        "--base-url", help="URL of the LLM endpoint (default: OLLAMAIL_LLM_BASE_URL)"
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "openai_compatible"),
        help="API of the endpoint (default: OLLAMAIL_LLM_PROVIDER)",
    )
    parser.add_argument("--embedding-model", help="embedding model for the RAG index")
    parser.add_argument(
        "--judge-model", help="also grade RAG answers with this model (LLM-as-judge)"
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("OLLAMAIL_EVAL_DATABASE_URL"),
        help="PostgreSQL with pgvector for the RAG stage, migrated; use a separate "
        "database, changes are rolled back (default: OLLAMAIL_EVAL_DATABASE_URL)",
    )
    parser.add_argument(
        "--language", action="append", choices=("de", "en"), help="only mails in this language"
    )
    parser.add_argument("--limit", type=int, help="evenly spaced sample of N mails")
    parser.add_argument(
        "--no-prefilter", action="store_true", help="send every mail to the triage model"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"seconds per model call before it counts as a timeout (default {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--output", type=Path, help="directory for report.json and report.md (else stdout)"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dataset = load_dataset().subset(
        languages=set(args.language) if args.language else None, limit=args.limit
    )
    options = RunOptions(
        models=args.model or [None],
        stages=args.stage or STAGES,
        base_url=args.base_url,
        provider=args.provider,
        embedding_model=args.embedding_model,
        judge_model=args.judge_model,
        database_url=args.database_url,
        use_prefilter=not args.no_prefilter,
        timeout=args.timeout,
        settings=Settings(),
    )
    report = asyncio.run(run(dataset, options))
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "report.json").write_text(report.to_json(), encoding="utf-8")
        (args.output / "report.md").write_text(report.to_markdown(), encoding="utf-8")
        print(f"wrote {args.output / 'report.md'} and report.json", file=sys.stderr)
    else:
        print(report.to_markdown())
    return 1 if any(m.error for m in report.models) else 0


if __name__ == "__main__":
    sys.exit(main())

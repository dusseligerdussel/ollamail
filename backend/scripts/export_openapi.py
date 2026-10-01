"""Export the OpenAPI schema without starting a server.

Usage (from ``backend/``)::

    uv run python -m scripts.export_openapi ../frontend/src/api/openapi.json
    uv run python -m scripts.export_openapi -        # to stdout

The frontend runs this via ``pnpm gen:api``; CI fails if the committed schema or the
generated client is out of date.
"""

import argparse
import sys
from pathlib import Path

from app.core.openapi import render_openapi
from app.main import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("output", help="target file, or '-' for stdout")
    args = parser.parse_args(argv)

    # Building the app does not connect to the database.
    text = render_openapi(create_app())
    if args.output == "-":
        sys.stdout.write(text)
    else:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

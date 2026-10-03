"""Liveness of the worker: a heartbeat file and its check.

The worker touches ``OLLAMAIL_WORKER_HEARTBEAT_FILE`` every
``OLLAMAIL_WORKER_HEARTBEAT_INTERVAL_SECONDS`` while its event loop and all job workers
run (``app.worker.run``). ``python -m app.core.heartbeat`` exits with 1 once the file is
older than four intervals or missing: the Compose healthcheck and the Kubernetes liveness
probe of the worker. A blocked event loop or a crashed job worker stops the heartbeat.
"""

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

from app.core.config import WorkerSettings

# Missed intervals before the worker counts as dead.
STALE_INTERVALS = 4


def touch(path: Path) -> None:
    path.write_text(f"{time.time():.0f}\n")


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)


def is_fresh(path: Path, max_age: float, *, now: float | None = None) -> bool:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - modified <= max_age


async def run_heartbeat(
    path: Path, interval: float, stop: asyncio.Event, alive: Callable[[], bool]
) -> None:
    """Touch ``path`` every ``interval`` seconds while ``alive()``; remove it on stop."""
    try:
        while not stop.is_set():
            if alive():
                touch(path)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
    finally:
        remove(path)


def check(settings: WorkerSettings) -> bool:
    max_age = settings.heartbeat_interval_seconds * STALE_INTERVALS
    return is_fresh(settings.heartbeat_file, max_age)


def main() -> int:
    # Only the worker settings: the probe must start fast and not need the other settings.
    if check(WorkerSettings()):
        return 0
    print("worker heartbeat missing or stale", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

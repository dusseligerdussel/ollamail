"""Worker liveness: heartbeat file and ``python -m app.core.heartbeat``."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import heartbeat
from app.core.config import WorkerSettings
from tests.conftest import BACKEND_DIR


def test_missing_or_old_file_is_not_fresh(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    assert not heartbeat.is_fresh(path, 60)

    heartbeat.touch(path)
    modified = path.stat().st_mtime

    assert heartbeat.is_fresh(path, 60, now=modified + 60)
    assert not heartbeat.is_fresh(path, 60, now=modified + 61)


def test_check_allows_four_intervals(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    settings = WorkerSettings(heartbeat_file=path, heartbeat_interval_seconds=10)
    heartbeat.touch(path)

    assert heartbeat.check(settings)
    old = time.time() - 41
    os.utime(path, (old, old))
    assert not heartbeat.check(settings)


async def test_heartbeat_runs_while_alive_and_is_removed_on_stop(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat"
    stop = asyncio.Event()
    alive = [True]
    task = asyncio.create_task(heartbeat.run_heartbeat(path, 0.01, stop, lambda: alive[0]))

    await asyncio.sleep(0.05)
    assert path.exists()
    alive[0] = False
    old = time.time() - 100
    os.utime(path, (old, old))
    await asyncio.sleep(0.05)
    # Not touched while a job worker is down.
    assert path.stat().st_mtime == pytest.approx(old)

    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert not path.exists()


@pytest.mark.parametrize("fresh", [True, False])
def test_probe_command(tmp_path: Path, fresh: bool) -> None:
    path = tmp_path / "heartbeat"
    if fresh:
        heartbeat.touch(path)
    env = {**os.environ, "OLLAMAIL_WORKER_HEARTBEAT_FILE": str(path)}

    result = subprocess.run(
        [sys.executable, "-m", "app.core.heartbeat"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == (0 if fresh else 1)

"""Fixtures for the todo export: a real CalDAV server (Radicale, started per test session
like slapd in the LDAP tests), an in-memory sink for the sync logic, and export targets.
All names, addresses and passwords are invented."""

import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, TodosSettings
from app.core.crypto import KeyRing, set_keyring
from app.todos.export.base import (
    RemoteTask,
    RemoteVersion,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    TaskData,
    TaskList,
    TodoSink,
)
from app.todos.export.models import ExportMode, TodoExportTarget
from app.todos.models import TodoStatus
from app.users.models import User

# Users of the test server (htpasswd, plain text).
CALDAV_USERS = {"erika": "erika-dav-secret", "lena": "lena-dav-secret"}


@dataclass(frozen=True)
class CalDAVServer:
    url: str

    def user_url(self, user: str) -> str:
        return f"{self.url}/{user}/"

    def client(self, user: str = "erika") -> httpx.Client:
        return httpx.Client(auth=(user, CALDAV_USERS[user]), timeout=10)

    def make_calendar(self, user: str, name: str, components: tuple[str, ...] = ("VTODO",)) -> str:
        """Create a calendar collection; returns its path."""
        path = f"/{user}/{name}/"
        comps = "".join(f'<C:comp name="{comp}"/>' for comp in components)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            f"<D:set><D:prop><D:displayname>{name.title()}</D:displayname>"
            f"<C:supported-calendar-component-set>{comps}</C:supported-calendar-component-set>"
            "</D:prop></D:set></C:mkcalendar>"
        )
        with self.client(user) as http:
            response = http.request("MKCALENDAR", f"{self.url}{path}", content=body)
        assert response.status_code == 201, response.status_code
        return path

    def get(self, path: str, user: str = "erika") -> httpx.Response:
        with self.client(user) as http:
            return http.get(f"{self.url}{path}")

    def put(self, path: str, body: str, user: str = "erika") -> httpx.Response:
        with self.client(user) as http:
            return http.put(
                f"{self.url}{path}",
                content=body.encode(),
                headers={"Content-Type": "text/calendar; charset=utf-8"},
            )

    def delete(self, path: str, user: str = "erika") -> httpx.Response:
        with self.client(user) as http:
            return http.delete(f"{self.url}{path}")


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    """Target credentials are encrypted with the process-wide key ring."""
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def caldav_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CalDAVServer]:
    """Radicale (a dev dependency) on a free local port."""
    root: Path = tmp_path_factory.mktemp("radicale")
    users = root / "users"
    users.write_text("".join(f"{name}:{secret}\n" for name, secret in CALDAV_USERS.items()))
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "radicale",
            "--server-hosts",
            f"127.0.0.1:{port}",
            "--auth-type",
            "htpasswd",
            "--auth-htpasswd-filename",
            str(users),
            "--auth-htpasswd-encryption",
            "plain",
            "--auth-delay",
            "0",
            "--rights-type",
            "owner_only",
            "--storage-filesystem-folder",
            str(root / "collections"),
            "--logging-level",
            "error",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                httpx.get(url, timeout=1)
                break
            except httpx.HTTPError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("Radicale did not start")
                time.sleep(0.1)
        yield CalDAVServer(url)
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def calendar(caldav_server: CalDAVServer) -> str:
    """A fresh task list of ``erika``."""
    return caldav_server.make_calendar("erika", f"tasks-{uuid.uuid4().hex[:8]}")


@dataclass
class StoredTask:
    data: TaskData
    etag: str
    status: TodoStatus
    modified_at: datetime | None


@dataclass
class FakeSink(TodoSink):
    """In-memory target system: lists ``list-1``/``list-2``, ETags count writes."""

    kind = "caldav"
    lists: list[TaskList] = field(
        default_factory=lambda: [TaskList("list-1", "Tasks"), TaskList("list-2", "Work")]
    )
    tasks: dict[str, StoredTask] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # Raised by every call while set.
    error: SinkError | None = None
    # Raised by ``push``/``update`` of these uids.
    reject: dict[str, SinkError] = field(default_factory=dict)
    closed: int = 0
    _version: int = 0

    def _check(self, name: str, key: str) -> None:
        self.calls.append((name, key))
        if self.error is not None:
            raise self.error

    def _store(self, remote_id: str, task: TaskData) -> RemoteVersion:
        self._version += 1
        etag = f'"{self._version}"'
        self.tasks[remote_id] = StoredTask(task, etag, task.status, task.modified_at)
        return RemoteVersion(remote_id, etag)

    def remote_id(self, list_id: str, uid: str) -> str:
        return f"{list_id}/{uid}"

    def edit_remotely(
        self, remote_id: str, status: TodoStatus, modified_at: datetime | None = None
    ) -> None:
        """A change in the target system's own app."""
        self._version += 1
        stored = self.tasks[remote_id]
        stored.status = status
        stored.etag = f'"{self._version}"'
        stored.modified_at = modified_at or datetime.now(UTC)

    async def list_task_lists(self) -> list[TaskList]:
        self._check("lists", "")
        return list(self.lists)

    async def push(self, list_id: str, task: TaskData) -> RemoteVersion:
        self._check("push", task.uid)
        if task.uid in self.reject:
            raise self.reject[task.uid]
        if list_id not in {item.id for item in self.lists}:
            raise SinkNotFoundError()
        return self._store(self.remote_id(list_id, task.uid), task)

    async def update(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        self._check("update", task.uid)
        if task.uid in self.reject:
            raise self.reject[task.uid]
        stored = self.tasks.get(remote_id)
        if stored is None:
            raise SinkNotFoundError()
        if etag is not None and etag != stored.etag:
            raise SinkConflictError()
        return self._store(remote_id, task)

    async def delete(self, list_id: str, remote_id: str) -> None:
        self._check("delete", remote_id)
        self.tasks.pop(remote_id, None)
        self.deleted.append(remote_id)

    async def changes(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        self._check("changes", list_id)
        result: dict[str, RemoteTask | None] = {}
        for remote_id, etag in known.items():
            stored = self.tasks.get(remote_id)
            if stored is None:
                result[remote_id] = None
            elif stored.etag != etag:
                result[remote_id] = RemoteTask(
                    remote_id, stored.etag, stored.status, stored.modified_at
                )
        return result

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
def fake_sink() -> FakeSink:
    return FakeSink()


def builder_for(sink: TodoSink) -> Any:
    def build(kind: str, config: Mapping[str, Any], settings: TodosSettings) -> TodoSink:
        return sink

    return build


EXPORT_SETTINGS = TodosSettings(export_sinks=["caldav"])


async def make_target(
    session: AsyncSession,
    user: User,
    *,
    mode: ExportMode = ExportMode.AUTO,
    list_id: str = "list-1",
    config: dict[str, Any] | None = None,
) -> TodoExportTarget:
    target = TodoExportTarget(
        user_id=user.id,
        sink="caldav",
        config=config or {"url": "https://dav.example.org/", "username": "erika", "password": "x"},
        list_id=list_id,
        list_name="Tasks",
        mode=mode,
    )
    session.add(target)
    await session.flush()
    return target

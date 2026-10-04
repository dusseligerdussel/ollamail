"""Integration tests: digest API, private podcast feed and audio with Range requests."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import feedparser
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, StorageSettings
from app.core.db import get_db
from app.digest.models import Digest, DigestLength, DigestStatus, DigestTrigger
from app.digest.router import get_enqueuer
from app.digest.storage import DigestStorage
from app.main import create_app
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.digest.conftest import make_mailbox

pytestmark = pytest.mark.db

AUDIO = bytes(range(256)) * 40  # 10 240 bytes
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


@pytest.fixture
def queued() -> list[uuid.UUID]:
    return []


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
async def app(
    settings: Settings, db_session: AsyncSession, queued: list[uuid.UUID], data_dir: Path
) -> AsyncIterator[FastAPI]:
    settings = settings.model_copy(
        update={"storage": StorageSettings.model_validate({"data_dir": data_dir})}
    )
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def enqueue(digest_id: uuid.UUID) -> None:
        queued.append(digest_id)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_enqueuer] = lambda: enqueue
    yield app
    await app.state.database.dispose()


@pytest.fixture
async def anonymous(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with api_client(app) as http:
        yield http


@pytest.fixture
async def erika(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "erika@example.org")
    async with api_client(app) as http:
        assert (await login(http, "erika@example.org")).status_code == 200
        yield http


@pytest.fixture
async def bob(app: FastAPI, db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await make_local_user(db_session, "bob@example.org")
    async with api_client(app) as http:
        assert (await login(http, "bob@example.org")).status_code == 200
        yield http


async def user(session: AsyncSession, email: str) -> User:
    found = await session.scalar(select(User).where(User.email == email))
    assert found is not None
    return found


async def ready_digest(
    session: AsyncSession, data_dir: Path, email: str = "erika@example.org"
) -> Digest:
    owner = await user(session, email)
    digest = Digest(
        user_id=owner.id,
        trigger=DigestTrigger.SCHEDULED,
        status=DigestStatus.READY,
        period_start=datetime(2026, 10, 1, 5, tzinfo=UTC),
        period_end=datetime(2026, 10, 2, 5, tzinfo=UTC),
        scheduled_for=datetime(2026, 10, 2, 5, tzinfo=UTC),
        language="en",
        length=DigestLength.NORMAL,
        title="Digest for Friday, October 2, 2026",
        script="# Digest\n\nAnna & Max need the report [1].\n\nThat's your digest.\n",
        duration_seconds=125.4,
        generated_at=datetime(2026, 10, 2, 5, 3, tzinfo=UTC),
    )
    session.add(digest)
    await session.flush()
    storage = DigestStorage(data_dir)
    target = storage.target(owner.id, digest.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    audio = {}
    for fmt in ("mp3", "opus"):
        path = target.with_name(f"{target.name}.{fmt}")
        path.write_bytes(AUDIO)
        audio[fmt] = {"path": storage.relative(path), "size_bytes": len(AUDIO)}
    digest.audio = audio
    await session.flush()
    return digest


def local_path(url: str) -> str:
    """Path for the test client: the app runs without the proxy's ``/api`` prefix."""
    assert url.startswith("https://test/api/")
    return url.removeprefix("https://test/api")


# -- settings ----------------------------------------------------------------------------


async def test_requires_sign_in(anonymous: AsyncClient) -> None:
    for path in ("/digests", "/digests/settings", "/digests/feed"):
        assert (await anonymous.get(path)).status_code == 401
    assert (await anonymous.post("/digests")).status_code == 401


async def test_default_settings(erika: AsyncClient) -> None:
    response = await erika.get("/digests/settings")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "delivery_time": "07:00:00",
        "timezone": None,
        "effective_timezone": "UTC",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "language": None,
        "effective_language": "en",
        "voice": None,
        "length": "normal",
        "mailbox_ids": None,
        "next_run_at": None,
        "feed": {"active": False, "created_at": None},
    }


async def test_voices(erika: AsyncClient, anonymous: AsyncClient, data_dir: Path) -> None:
    voices = data_dir / "tts" / "voices" / "piper"
    voices.mkdir(parents=True)
    for name in ("de_DE-kerstin-low", "de_DE-thorsten-medium"):
        (voices / f"{name}.onnx").write_bytes(b"model")
        (voices / f"{name}.onnx.json").write_text("{}")

    response = await erika.get("/digests/voices")

    assert response.status_code == 200
    assert response.json() == [
        {"id": "de_DE-kerstin-low", "language": "de", "default": False, "installed": True},
        {"id": "de_DE-thorsten-medium", "language": "de", "default": True, "installed": True},
        {"id": "en_US-ljspeech-medium", "language": "en", "default": True, "installed": False},
    ]
    assert (await anonymous.get("/digests/voices")).status_code == 401


async def test_update_settings(erika: AsyncClient, db_session: AsyncSession) -> None:
    mailbox = await make_mailbox(db_session, await user(db_session, "erika@example.org"), "e@x.org")

    response = await erika.patch(
        "/digests/settings",
        json={
            "enabled": True,
            "delivery_time": "06:45:30",
            "timezone": "Europe/Berlin",
            "weekdays": [4, 0, 0, 2],
            "language": "de",
            "voice": "de_DE-thorsten-medium",
            "length": "short",
            "mailbox_ids": [str(mailbox.id)],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["delivery_time"] == "06:45:00"
    assert body["weekdays"] == [0, 2, 4]
    assert (body["effective_timezone"], body["effective_language"]) == ("Europe/Berlin", "de")
    assert body["mailbox_ids"] == [str(mailbox.id)]
    next_run = datetime.fromisoformat(body["next_run_at"])
    assert next_run > datetime.now(UTC)
    assert (
        next_run.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).strftime("%H:%M")
        == "06:45"
    )
    # Partial update; null resets to the default.
    response = await erika.patch("/digests/settings", json={"timezone": None, "length": "normal"})
    assert (response.json()["timezone"], response.json()["length"]) == (None, "normal")
    assert response.json()["weekdays"] == [0, 2, 4]


@pytest.mark.parametrize(
    "body",
    [
        {"timezone": "Mars/Base"},
        {"weekdays": [7]},
        {"language": "fr"},
        {"voice": "../../etc/passwd"},
        {"enabled": None},
        {"unknown": 1},
    ],
)
async def test_invalid_settings(erika: AsyncClient, body: dict[str, object]) -> None:
    assert (await erika.patch("/digests/settings", json=body)).status_code == 422


async def test_only_offered_voices_can_be_picked(erika: AsyncClient, data_dir: Path) -> None:
    # Formally valid, but neither installed, a default nor allowlisted: picking it must
    # not make the server download it (#191).
    response = await erika.patch("/digests/settings", json={"voice": "de_DE-pavoque-low"})
    assert response.status_code == 422
    assert response.json()["error_code"] == "unknown_voice"

    voices = data_dir / "tts" / "voices" / "piper"
    voices.mkdir(parents=True)
    (voices / "de_DE-pavoque-low.onnx").write_bytes(b"model")
    (voices / "de_DE-pavoque-low.onnx.json").write_text("{}")
    response = await erika.patch("/digests/settings", json={"voice": "de_DE-pavoque-low"})
    assert response.status_code == 200
    assert response.json()["voice"] == "de_DE-pavoque-low"


async def test_foreign_mailbox_is_rejected(
    erika: AsyncClient, bob: AsyncClient, db_session: AsyncSession
) -> None:
    foreign = await make_mailbox(db_session, await user(db_session, "bob@example.org"), "b@x.org")

    response = await erika.patch("/digests/settings", json={"mailbox_ids": [str(foreign.id)]})

    assert response.status_code == 422


# -- digests -----------------------------------------------------------------------------


async def test_create_digest_now(erika: AsyncClient, queued: list[uuid.UUID]) -> None:
    response = await erika.post("/digests")

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["trigger"], body["script"]) == ("pending", "manual", None)
    assert queued == [uuid.UUID(body["id"])]
    # Only one at a time.
    conflict = await erika.post("/digests")
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "digest_in_progress"


async def test_list_get_delete(
    erika: AsyncClient, db_session: AsyncSession, data_dir: Path
) -> None:
    digest = await ready_digest(db_session, data_dir)

    listed = await erika.get("/digests")
    assert [item["id"] for item in listed.json()] == [str(digest.id)]
    assert listed.json()[0]["audio_formats"] == ["mp3", "opus"]
    assert "script" not in listed.json()[0]
    fetched = await erika.get(f"/digests/{digest.id}")
    assert fetched.json()["script"].startswith("# Digest")

    deleted = await erika.delete(f"/digests/{digest.id}")

    assert deleted.status_code == 204
    assert (await erika.get(f"/digests/{digest.id}")).status_code == 404
    assert not list((data_dir / "digests").rglob(f"{digest.id}.*"))


async def test_other_users_digests_are_invisible(
    erika: AsyncClient, bob: AsyncClient, db_session: AsyncSession, data_dir: Path
) -> None:
    digest = await ready_digest(db_session, data_dir, "bob@example.org")

    assert (await erika.get("/digests")).json() == []
    for request in (
        erika.get(f"/digests/{digest.id}"),
        erika.get(f"/digests/{digest.id}/audio.mp3"),
        erika.delete(f"/digests/{digest.id}"),
    ):
        assert (await request).status_code == 404
    assert (await bob.get(f"/digests/{digest.id}")).status_code == 200


async def test_audio_supports_range_requests(
    erika: AsyncClient, db_session: AsyncSession, data_dir: Path
) -> None:
    digest = await ready_digest(db_session, data_dir)

    full = await erika.get(f"/digests/{digest.id}/audio.mp3")
    assert full.status_code == 200
    assert full.content == AUDIO
    assert full.headers["content-type"] == "audio/mpeg"
    assert full.headers["accept-ranges"] == "bytes"

    part = await erika.get(f"/digests/{digest.id}/audio.opus", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == AUDIO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    assert part.headers["content-type"] == "audio/ogg"

    assert (await erika.get(f"/digests/{digest.id}/audio.wav")).status_code == 422


# -- podcast feed ------------------------------------------------------------------------


async def test_feed_lifecycle(
    erika: AsyncClient, anonymous: AsyncClient, db_session: AsyncSession, data_dir: Path
) -> None:
    digest = await ready_digest(db_session, data_dir)
    # Not ready yet: not in the feed.
    await db_session.merge(
        Digest(
            user_id=digest.user_id,
            trigger=DigestTrigger.MANUAL,
            status=DigestStatus.SUMMARIZING,
            period_start=digest.period_start,
            period_end=digest.period_end,
            language="en",
            length=DigestLength.NORMAL,
        )
    )
    assert (await erika.get("/digests/feed")).json() == {"active": False, "created_at": None}

    created = await erika.post("/digests/feed")

    assert created.status_code == 201
    feed_url = created.json()["feed_url"]
    status = (await erika.get("/digests/feed")).json()
    assert status["active"] is True and "feed_url" not in status

    response = await anonymous.get(local_path(feed_url))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/rss+xml; charset=utf-8"
    assert "noindex" in response.headers["x-robots-tag"]
    assert (await anonymous.head(local_path(feed_url))).status_code == 200

    parsed = feedparser.parse(response.content)
    assert not parsed.bozo, parsed.get("bozo_exception")
    assert parsed.version == "rss20"
    assert parsed.feed.title == "ollamail: Daily digest"
    assert parsed.feed.language == "en"
    assert parsed.feed.links[0].href == "https://test/"
    assert [link.href for link in parsed.feed.links if link.rel == "self"] == [feed_url]
    [entry] = parsed.entries
    assert entry.title == "Digest for Friday, October 2, 2026"
    assert entry.id == f"ollamail-digest-{digest.id}"
    assert entry.summary == "Anna & Max need the report.\n\nThat's your digest."
    assert entry.itunes_duration == "00:02:05"
    [enclosure] = entry.enclosures
    assert (enclosure.type, enclosure.length) == ("audio/mpeg", str(len(AUDIO)))
    assert entry.published_parsed[:5] == (2026, 10, 2, 5, 3)

    # Strict checks of what podcast apps rely on.
    root = ET.fromstring(response.content)
    channel = root.find("channel")
    assert root.tag == "rss" and root.get("version") == "2.0" and channel is not None
    for tag in ("title", "link", "description", "language", f"{ITUNES}image", f"{ITUNES}block"):
        assert channel.find(tag) is not None, tag
    assert channel.findtext(f"{ITUNES}block") == "Yes"
    item = channel.find("item")
    assert item is not None
    assert item.find("guid").get("isPermaLink") == "false"  # type: ignore[union-attr]

    # Audio through the feed: token protected, with Range requests.
    audio_url = local_path(enclosure.href)
    assert audio_url == f"/feeds/{feed_url.rsplit('/', 1)[1].removesuffix('.xml')}/{digest.id}.mp3"
    assert (await anonymous.get(audio_url)).content == AUDIO
    head = await anonymous.head(audio_url)
    assert (head.status_code, head.headers["content-length"]) == (200, str(len(AUDIO)))
    ranged = await anonymous.get(audio_url, headers={"Range": "bytes=-16"})
    assert (ranged.status_code, ranged.content) == (206, AUDIO[-16:])

    # A new URL replaces the old one; revoking disables both feed and audio.
    rotated = (await erika.post("/digests/feed")).json()["feed_url"]
    assert (await anonymous.get(local_path(feed_url))).status_code == 404
    assert (await anonymous.get(audio_url)).status_code == 404
    assert (await anonymous.get(local_path(rotated))).status_code == 200

    assert (await erika.delete("/digests/feed")).status_code == 204
    assert (await anonymous.get(local_path(rotated))).status_code == 404
    assert (await erika.get("/digests/feed")).json()["active"] is False


async def test_feed_tokens_do_not_cross_users(
    erika: AsyncClient,
    bob: AsyncClient,
    anonymous: AsyncClient,
    db_session: AsyncSession,
    data_dir: Path,
) -> None:
    foreign = await ready_digest(db_session, data_dir, "bob@example.org")
    feed_url = local_path((await erika.post("/digests/feed")).json()["feed_url"])
    token = feed_url.removeprefix("/feeds/").removesuffix(".xml")

    assert feedparser.parse((await anonymous.get(feed_url)).content).entries == []
    assert (await anonymous.get(f"/feeds/{token}/{foreign.id}.mp3")).status_code == 404


@pytest.mark.parametrize("token", ["nope", "A" * 43, "../../etc/passwd"])
async def test_unknown_token_is_404(anonymous: AsyncClient, token: str) -> None:
    assert (await anonymous.get(f"/feeds/{token}.xml")).status_code == 404


async def test_feed_of_inactive_user_is_404(
    erika: AsyncClient, anonymous: AsyncClient, db_session: AsyncSession
) -> None:
    feed_url = local_path((await erika.post("/digests/feed")).json()["feed_url"])
    (await user(db_session, "erika@example.org")).is_active = False
    await db_session.flush()

    assert (await anonymous.get(feed_url)).status_code == 404

"""Digest API (signed in) and the private podcast feed (token in the URL).

Users only ever see their own digests; another user's digest behaves like a missing one
(404). The feed and its audio URLs need no session: the token is the credential. A wrong,
revoked or replaced token yields 404, like a feed that never existed.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, CurrentUserDep, SettingsDep
from app.auth.redirect_flow import api_url, public_origin
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.jobs import JobQueue
from app.digest import feed, schedule, service, tasks, texts
from app.digest.content import selected_mailboxes
from app.digest.models import Digest, DigestUserSettings
from app.digest.schemas import (
    AudioFormatName,
    DigestRead,
    DigestSettingsRead,
    DigestSettingsUpdate,
    DigestSummary,
    FeedCreated,
    FeedStatus,
)
from app.digest.storage import DigestStorage
from app.users.models import User

router = APIRouter(
    prefix="/digests",
    tags=["digests"],
    responses={401: {"description": "Not signed in"}},
)
# Public: podcast apps cannot sign in. Not part of the OpenAPI schema (no client uses it).
feed_router = APIRouter(prefix="/feeds", tags=["feeds"], include_in_schema=False)

DbDep = Annotated[AsyncSession, Depends(get_db)]
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"description": "No such digest"}}
# Digests listed in the feed.
FEED_ITEMS = 50
AUDIO_HEADERS = {"Cache-Control": "private, max-age=86400", "X-Robots-Tag": "noindex"}
FEED_HEADERS = {"Cache-Control": "private, no-cache", "X-Robots-Tag": "noindex"}

Enqueuer = Callable[[uuid.UUID], Awaitable[None]]


def get_storage(settings: SettingsDep) -> DigestStorage:
    return DigestStorage(settings.storage.data_dir)


def get_enqueuer(request: Request) -> Enqueuer:
    """Queues the text job of a committed digest."""
    queue: JobQueue = request.app.state.job_queue

    async def enqueue(digest_id: uuid.UUID) -> None:
        await queue.ensure_open()
        await tasks.enqueue_generation(digest_id)

    return enqueue


StorageDep = Annotated[DigestStorage, Depends(get_storage)]
EnqueuerDep = Annotated[Enqueuer, Depends(get_enqueuer)]


def _now() -> datetime:
    return datetime.now(UTC)


async def _digest(db: AsyncSession, user_id: uuid.UUID, digest_id: uuid.UUID) -> Digest:
    digest = await service.get_digest(db, user_id, digest_id)
    if digest is None:
        raise ProblemError(404, detail="Digest not found.")
    return digest


def _settings_read(user: User, row: DigestUserSettings | None) -> DigestSettingsRead:
    current = service.effective(user, row)
    next_run = None
    if current.enabled:
        next_run = schedule.next_slot(_now(), current.delivery_time, current.zone, current.weekdays)
    return DigestSettingsRead(
        enabled=current.enabled,
        delivery_time=current.delivery_time,
        timezone=row.timezone if row is not None else None,
        effective_timezone=current.timezone,
        weekdays=current.weekdays,
        language=texts.language_of(row.language) if row is not None and row.language else None,
        effective_language=current.language,
        voice=current.voice,
        length=current.length,
        mailbox_ids=current.mailbox_ids,
        next_run_at=next_run,
        feed=_feed_status(row),
    )


def _feed_status(row: DigestUserSettings | None) -> FeedStatus:
    if row is None or row.feed_token_hash is None:
        return FeedStatus(active=False, created_at=None)
    return FeedStatus(active=True, created_at=row.feed_token_created_at)


# -- settings ----------------------------------------------------------------------------


@router.get("/settings")
async def get_digest_settings(user: CurrentUserDep, db: DbDep) -> DigestSettingsRead:
    """Own digest settings with defaults filled in."""
    return _settings_read(user, await service.settings_row(db, user.id))


@router.patch("/settings", responses={422: {"description": "Invalid value or mailbox"}})
async def update_digest_settings(
    body: DigestSettingsUpdate, user: CurrentUserDep, db: DbDep
) -> DigestSettingsRead:
    """Change own digest settings. A changed schedule starts with the next slot."""
    changes = body.model_dump(exclude_unset=True)
    for required in ("enabled", "delivery_time", "weekdays", "length"):
        if required in changes and changes[required] is None:
            raise ProblemError(422, detail=f"{required} cannot be null.")
    mailbox_ids = changes.get("mailbox_ids")
    if mailbox_ids is not None:
        own = await selected_mailboxes(db, user.id, mailbox_ids)
        if set(own) != set(mailbox_ids):
            raise ProblemError(422, detail="Unknown mailbox.", error_code="unknown_mailbox")
        changes["mailbox_ids"] = sorted(set(mailbox_ids))
    row = await service.update_settings(db, user, changes, now=_now())
    await db.commit()
    return _settings_read(user, row)


# -- podcast feed ------------------------------------------------------------------------


@router.get("/feed")
async def get_feed(current: CurrentSessionDep, db: DbDep) -> FeedStatus:
    """Whether the private podcast feed is enabled (the URL is only shown on creation)."""
    return _feed_status(await service.settings_row(db, current.user_id))


@router.post("/feed", status_code=status.HTTP_201_CREATED)
async def create_feed(
    request: Request, current: CurrentSessionDep, db: DbDep, settings: SettingsDep
) -> FeedCreated:
    """Create a new secret feed URL. An existing feed URL stops working immediately."""
    now = _now()
    token = await service.rotate_feed_token(db, current.user_id, now=now)
    await db.commit()
    return FeedCreated(feed_url=api_url(settings, request, f"/feeds/{token}.xml"), created_at=now)


@router.delete("/feed", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_feed(current: CurrentSessionDep, db: DbDep) -> Response:
    """Disable the feed; its URL and all audio URLs in it return 404 from now on."""
    await service.revoke_feed_token(db, current.user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- digests -----------------------------------------------------------------------------


@router.get("")
async def list_digests(
    current: CurrentSessionDep,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[DigestSummary]:
    """Own digests, newest first."""
    digests = await service.list_digests(db, current.user_id, limit=limit, offset=offset)
    return [DigestSummary.model_validate(digest) for digest in digests]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "A digest is already being generated"}},
)
async def create_digest(
    user: CurrentUserDep, db: DbDep, settings: SettingsDep, enqueue: EnqueuerDep
) -> DigestRead:
    """Generate a digest of the mails since the last one, now."""
    if await service.in_progress(db, user.id):
        raise ProblemError(
            409, detail="A digest is already being generated.", error_code="digest_in_progress"
        )
    digest = await service.create_manual(db, user, now=_now(), config=settings.digest)
    await db.commit()
    await enqueue(digest.id)
    await db.refresh(digest)
    return DigestRead.model_validate(digest)


@router.get("/{digest_id}", responses=NOT_FOUND)
async def get_digest(digest_id: uuid.UUID, current: CurrentSessionDep, db: DbDep) -> DigestRead:
    """A digest with its script and the mails it refers to."""
    return DigestRead.model_validate(await _digest(db, current.user_id, digest_id))


@router.delete("/{digest_id}", status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_digest(
    digest_id: uuid.UUID, current: CurrentSessionDep, db: DbDep, storage: StorageDep
) -> Response:
    """Delete a digest and its audio files."""
    digest = await _digest(db, current.user_id, digest_id)
    await service.delete_digest(db, digest, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _audio_response(digest: Digest, fmt: str, storage: DigestStorage) -> FileResponse:
    entry = digest.audio.get(fmt)
    if not entry:
        raise ProblemError(404, detail="Audio not found.")
    path: Path = storage.resolve(entry["path"])
    if not path.is_file():
        raise ProblemError(404, detail="Audio not found.")
    # Starlette answers Range requests (206) and HEAD itself.
    return FileResponse(
        path,
        media_type=feed.ENCLOSURE_TYPES[fmt],
        filename=f"digest-{digest.id}.{fmt}",
        content_disposition_type="inline",
        headers=AUDIO_HEADERS,
    )


@router.get(
    "/{digest_id}/audio.{fmt}",
    response_class=FileResponse,
    responses={
        200: {"content": {"audio/mpeg": {}, "audio/ogg": {}}, "description": "Audio file"},
        206: {"description": "Partial content (Range request)"},
        **NOT_FOUND,
    },
)
async def get_audio(
    digest_id: uuid.UUID,
    fmt: AudioFormatName,
    current: CurrentSessionDep,
    db: DbDep,
    storage: StorageDep,
) -> FileResponse:
    """The audio of a digest for the web player; supports Range requests."""
    return _audio_response(await _digest(db, current.user_id, digest_id), fmt, storage)


# -- public feed -------------------------------------------------------------------------


async def _feed_user(db: AsyncSession, token: str) -> User:
    user = await service.feed_owner(db, token)
    if user is None:
        raise ProblemError(404)
    return user


@feed_router.api_route("/{token}.xml", methods=["GET", "HEAD"])
async def podcast_feed(token: str, request: Request, db: DbDep, settings: SettingsDep) -> Response:
    user = await _feed_user(db, token)
    row = await service.settings_row(db, user.id)
    language = service.effective(user, row).language
    origin = public_origin(settings, request)
    links = feed.FeedLinks(
        site=origin + "/",
        feed=api_url(settings, request, f"/feeds/{token}.xml"),
        audio_base=api_url(settings, request, f"/feeds/{token}"),
        image=origin + "/icons/icon-512.png",
    )
    digests = await service.feed_digests(db, user.id, FEED_ITEMS)
    body = feed.render(digests, links=links, language=language, built_at=_now())
    return Response(body, media_type=feed.MEDIA_TYPE, headers=FEED_HEADERS)


@feed_router.api_route("/{token}/{digest_id}.{fmt}", methods=["GET", "HEAD"])
async def podcast_audio(
    token: str,
    digest_id: uuid.UUID,
    fmt: AudioFormatName,
    db: DbDep,
    storage: StorageDep,
) -> FileResponse:
    user = await _feed_user(db, token)
    digest = await service.get_digest(db, user.id, digest_id)
    if digest is None:
        raise ProblemError(404)
    return _audio_response(digest, fmt, storage)

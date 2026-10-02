"""Digest service against PostgreSQL with fake LLM and fake TTS: period, content selection,
scheduling in the user's time zone (including DST), generation, retention."""

import os
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DigestSettings
from app.digest import service
from app.digest.content import collect
from app.digest.models import Digest, DigestStatus, DigestTrigger, DigestUserSettings
from app.digest.storage import DigestStorage
from app.mail.models import FolderRole, Mailbox
from app.todos.models import Todo, TodoStatus
from app.users.models import User
from tests.digest.conftest import DigestData, FakeLLM, FakeTTS, make_mailbox, utc
from tests.factories import make_user

pytestmark = pytest.mark.db

# Friday, 2 October 2026, 07:00 in Berlin (summer time) = 05:00 UTC.
SLOT = utc(2026, 10, 2, 5, 0)


async def enable(session: AsyncSession, user: User, **values: object) -> DigestUserSettings:
    row = await service.update_settings(
        session,
        user,
        {"enabled": True, "delivery_time": time(7, 0), **values},
        now=SLOT - timedelta(minutes=2),
    )
    await session.flush()
    return row


async def due(session: AsyncSession, now: datetime, config: DigestSettings) -> list[uuid.UUID]:
    return await service.schedule_due(session, now=now, config=config)


async def test_period_runs_from_the_previous_digest(
    data: DigestData, config: DigestSettings
) -> None:
    first = await service.create_manual(data.session, data.user, now=SLOT, config=config)
    assert (first.period_start, first.period_end) == (SLOT - timedelta(hours=24), SLOT)

    later = SLOT + timedelta(hours=3)
    second = await service.create_manual(data.session, data.user, now=later, config=config)
    assert (second.period_start, second.period_end) == (SLOT, later)

    # A failed digest does not count: its mails go into the next one.
    second.status = DigestStatus.FAILED
    await data.session.flush()
    third = await service.create_manual(data.session, data.user, now=later, config=config)
    assert third.period_start == SLOT


async def test_content_is_weighted_by_triage(data: DigestData, config: DigestSettings) -> None:
    start, end = SLOT - timedelta(hours=24), SLOT
    hour = timedelta(hours=1)
    info = await data.mail("Minutes", received_at=start + hour, category="info")
    action = await data.mail("Approve", received_at=start + 2 * hour, category="action_required")
    important = await data.mail("Contract", received_at=start + 3 * hour, category="important")
    untriaged = await data.mail("Hello", received_at=start + 4 * hour)
    await data.mail("Weekly", received_at=start + hour, category="newsletter", sender="News Co")
    await data.mail("Login", received_at=start + hour, category="notification", sender="Shop")
    await data.mail("Win!", received_at=start + hour, category="spam")
    # Outside the period, in excluded folders, in another user's mailbox: not included.
    await data.mail("Old", received_at=start - timedelta(seconds=1), category="important")
    await data.mail("At end", received_at=end, category="important")
    await data.mail("Sent", received_at=start + hour, role=FolderRole.SENT)
    await data.mail("Junk", received_at=start + hour, role=FolderRole.JUNK)
    other = await make_user(data.session)
    foreign = await make_mailbox(data.session, other, "other@example.org")
    await data.mail("Foreign", received_at=start + hour, mailbox=foreign)

    content = await collect(
        data.session,
        data.user.id,
        [data.mailbox.id],
        start,
        end,
        today=date(2026, 10, 2),
        settings=config,
    )

    assert [mail.message_id for mail in content.mails] == [
        action.id,
        important.id,
        untriaged.id,
        info.id,
    ]
    assert content.bulk == {"newsletter": ["News Co"], "notification": ["Shop"]}
    assert content.message_count == 6


async def test_only_the_most_important_mails_are_summarised(data: DigestData) -> None:
    start = SLOT - timedelta(hours=24)
    for minute in range(5):
        await data.mail(f"Info {minute}", received_at=start + timedelta(minutes=minute))
    urgent = await data.mail("Urgent", received_at=start, category="important", priority=1)

    content = await collect(
        data.session,
        data.user.id,
        [data.mailbox.id],
        start,
        SLOT,
        today=date(2026, 10, 2),
        settings=DigestSettings(max_messages=3),
    )

    assert content.mails[0].message_id == urgent.id
    assert (len(content.mails), content.more_mails, content.message_count) == (3, 3, 6)


async def test_todos_new_and_due_in_the_users_time_zone(
    data: DigestData, config: DigestSettings
) -> None:
    start = SLOT - timedelta(hours=24)
    friday = date(2026, 10, 2)
    rows = {
        "new": Todo(user_id=data.user.id, title="New", created_at=start + timedelta(hours=1)),
        "overdue": Todo(user_id=data.user.id, title="Overdue", due_date=friday - timedelta(2)),
        "today": Todo(user_id=data.user.id, title="Today", due_date=friday),
        "tomorrow": Todo(user_id=data.user.id, title="Tomorrow", due_date=friday + timedelta(1)),
        "later": Todo(user_id=data.user.id, title="Later", due_date=friday + timedelta(2)),
        "done": Todo(user_id=data.user.id, title="Done", due_date=friday, status=TodoStatus.DONE),
    }
    for todo in rows.values():
        todo.created_at = todo.created_at or start - timedelta(days=3)
        data.session.add(todo)
    await data.session.flush()

    content = await collect(
        data.session, data.user.id, [data.mailbox.id], start, SLOT, today=friday, settings=config
    )

    assert [t.title for t in content.new_todos] == ["New"]
    assert [t.title for t in content.due_todos] == ["Overdue", "Today", "Tomorrow"]


async def test_scheduler_creates_one_digest_per_slot(
    data: DigestData, config: DigestSettings
) -> None:
    await enable(data.session, data.user)

    assert await due(data.session, SLOT - timedelta(minutes=1), config) == []
    created = await service.schedule_due(
        data.session, now=SLOT + timedelta(seconds=30), config=config
    )
    again = await due(data.session, SLOT + timedelta(minutes=1), config)

    assert len(created) == 1 and again == []
    digest = await data.session.get(Digest, created[0])
    assert digest is not None
    assert (digest.trigger, digest.status) == (DigestTrigger.SCHEDULED, DigestStatus.PENDING)
    assert (digest.scheduled_for, digest.period_end) == (SLOT, SLOT)
    assert (digest.language, digest.mailbox_ids) == ("de", [data.mailbox.id])


async def test_scheduler_follows_dst_in_the_users_time_zone(
    data: DigestData, config: DigestSettings
) -> None:
    """07:00 Berlin is 05:00 UTC on Saturday 24 October and 06:00 UTC on Sunday 25 October."""
    row = await enable(data.session, data.user)
    row.last_scheduled_for = utc(2026, 10, 23, 5, 0)
    await data.session.flush()

    found = []
    now = utc(2026, 10, 24, 0, 0)
    while now < utc(2026, 10, 26, 0, 0):
        found += await due(data.session, now, config)
        now += timedelta(minutes=10)

    digests = [await data.session.get(Digest, digest_id) for digest_id in found]
    assert [d.scheduled_for for d in digests if d] == [utc(2026, 10, 24, 5), utc(2026, 10, 25, 6)]
    # The second digest continues where the first ended (25 hours across the change).
    assert digests[1] is not None and digests[1].period_start == utc(2026, 10, 24, 5)


async def test_scheduler_skips_disabled_and_inactive_users(
    data: DigestData, config: DigestSettings
) -> None:
    await enable(data.session, data.user)
    data.user.is_active = False
    other = await make_user(data.session, timezone="Europe/Berlin")
    await enable(data.session, other, enabled=False)

    assert await due(data.session, SLOT + timedelta(hours=1), config) == []


async def test_changed_settings_do_not_fire_a_past_slot(
    data: DigestData, config: DigestSettings
) -> None:
    # Enabled at 09:00 local time for 07:00: the first digest comes tomorrow.
    await service.update_settings(
        data.session, data.user, {"enabled": True}, now=SLOT + timedelta(hours=2)
    )

    assert await due(data.session, SLOT + timedelta(hours=3), config) == []
    assert await due(data.session, SLOT + timedelta(days=1), config) != []


async def test_generate_and_synthesize(
    data: DigestData, fake_llm: FakeLLM, fake_tts: FakeTTS, tmp_path: Path, config: DigestSettings
) -> None:
    start = SLOT - timedelta(hours=24)
    first = await data.mail(
        "Contract", received_at=start + timedelta(hours=2), category="important"
    )
    second = await data.mail("Minutes", received_at=start + timedelta(hours=3), category="info")
    await data.mail("Weekly", received_at=start + timedelta(hours=1), category="newsletter")
    digest = await service.create_manual(data.session, data.user, now=SLOT, config=config)

    needs_audio = await service.generate_text(
        data.session, digest.id, llm=fake_llm.gateway, config=config
    )

    assert needs_audio
    await data.session.refresh(digest)
    assert digest.status is DigestStatus.SYNTHESIZING
    assert digest.title == "Digest vom Freitag, 2. Oktober 2026"
    assert digest.script is not None
    assert "The mails are summarised here [1, 2]." in digest.script
    assert "Außerdem kam ein Newsletter von Max Example." in digest.script
    assert digest.references == [
        {"ref": 1, "message_id": str(first.id), "mailbox_id": str(data.mailbox.id)},
        {"ref": 2, "message_id": str(second.id), "mailbox_id": str(data.mailbox.id)},
    ]
    assert (digest.message_count, digest.model) == (3, "chat:1b")
    # German digest: the prompts are German.
    assert "Notizen" in fake_llm.provider.calls[0].messages[0].content

    storage = DigestStorage(tmp_path)
    await service.synthesize_audio(
        data.session, digest.id, tts=fake_tts.service, storage=storage, config=config
    )

    await data.session.refresh(digest)
    assert digest.status is DigestStatus.READY
    assert sorted(digest.audio) == ["mp3", "opus"]
    path = storage.resolve(digest.audio["mp3"]["path"])
    assert path == tmp_path / "digests" / str(data.user.id) / f"{digest.id}.mp3"
    assert path.stat().st_size == digest.audio["mp3"]["size_bytes"] > 0
    assert digest.duration_seconds is not None and digest.duration_seconds > 0
    spoken = " ".join(text for text, _, _ in fake_tts.engine.calls)
    assert "[1" not in spoken and "Digest vom" not in spoken
    assert {voice for _, voice, _ in fake_tts.engine.calls} == {"de_DE-thorsten-medium"}

    # Running a step again changes nothing (idempotent jobs).
    calls = len(fake_llm.provider.calls)
    assert not await service.generate_text(
        data.session, digest.id, llm=fake_llm.gateway, config=config
    )
    await service.synthesize_audio(
        data.session, digest.id, tts=fake_tts.service, storage=storage, config=config
    )
    assert len(fake_llm.provider.calls) == calls


async def test_empty_digest_needs_no_model(
    data: DigestData, fake_llm: FakeLLM, config: DigestSettings
) -> None:
    digest = await service.create_manual(data.session, data.user, now=SLOT, config=config)

    await service.generate_text(data.session, digest.id, llm=fake_llm.gateway, config=config)

    assert fake_llm.provider.calls == []
    assert digest.script is not None
    assert "keine neuen Mails" in digest.script


async def test_text_only_without_audio(data: DigestData, fake_llm: FakeLLM) -> None:
    config = DigestSettings(audio_enabled=False)
    digest = await service.create_manual(data.session, data.user, now=SLOT, config=config)

    assert not await service.generate_text(
        data.session, digest.id, llm=fake_llm.gateway, config=config
    )
    assert digest.status is DigestStatus.READY


async def test_cleanup_enforces_retention_with_files(
    data: DigestData, tmp_path: Path, config: DigestSettings
) -> None:
    storage = DigestStorage(tmp_path)
    old = await service.create_manual(data.session, data.user, now=SLOT, config=config)
    new = await service.create_manual(data.session, data.user, now=SLOT, config=config)
    old.created_at = SLOT - timedelta(days=31)
    await data.session.flush()
    for digest in (old, new):
        target = storage.target(data.user.id, digest.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix(".mp3").write_bytes(b"audio")

    result = await service.cleanup(data.session, storage, now=SLOT, retention_days=30)

    assert result.expired == 1
    assert await data.session.get(Digest, old.id) is None
    assert await data.session.get(Digest, new.id) is not None
    assert not storage.target(data.user.id, old.id).with_suffix(".mp3").exists()
    assert storage.target(data.user.id, new.id).with_suffix(".mp3").exists()


async def test_cleanup_deletes_digests_of_removed_mailboxes(
    data: DigestData, tmp_path: Path, config: DigestSettings
) -> None:
    digest = await service.create_manual(data.session, data.user, now=SLOT, config=config)
    await data.session.delete(await data.session.get(Mailbox, data.mailbox.id))
    await data.session.flush()

    result = await service.cleanup(
        data.session, DigestStorage(tmp_path), now=SLOT, retention_days=30
    )

    assert result.orphaned == 1
    assert await data.session.get(Digest, digest.id) is None


async def test_cleanup_removes_files_without_digest(
    data: DigestData, tmp_path: Path, config: DigestSettings
) -> None:
    storage = DigestStorage(tmp_path)
    kept = await service.create_manual(data.session, data.user, now=SLOT, config=config)
    gone_user = storage.base / "0192f3c4-0000-7000-8000-000000000000"
    gone_user.mkdir(parents=True)
    (gone_user / "x.mp3").write_bytes(b"audio")
    own = storage.target(data.user.id, kept.id).with_suffix(".mp3")
    stray = storage.target(data.user.id, kept.id).with_name(
        "0192f3c4-0000-7000-8000-00000000abcd.mp3"
    )
    own.parent.mkdir(parents=True)
    own.write_bytes(b"audio")
    stray.write_bytes(b"audio")
    old = (SLOT - timedelta(days=1)).timestamp()
    os.utime(stray, (old, old))

    result = await service.cleanup(data.session, storage, now=SLOT, retention_days=30)

    assert result.files == 2
    assert not gone_user.exists() and not stray.exists() and own.exists()


async def test_feed_token_is_stored_as_hash(data: DigestData) -> None:
    token = await service.rotate_feed_token(data.session, data.user.id, now=SLOT)

    row = await data.session.scalar(
        select(DigestUserSettings).where(DigestUserSettings.user_id == data.user.id)
    )
    assert row is not None and row.feed_token_hash is not None
    assert token not in row.feed_token_hash and len(row.feed_token_hash) == 64
    assert (await service.feed_owner(data.session, token)) is data.user

    replaced = await service.rotate_feed_token(data.session, data.user.id, now=SLOT)
    assert await service.feed_owner(data.session, token) is None
    assert await service.feed_owner(data.session, replaced) is data.user
    assert await service.revoke_feed_token(data.session, data.user.id)
    assert await service.feed_owner(data.session, replaced) is None

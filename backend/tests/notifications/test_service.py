"""Who hears about a newly triaged message (#149)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import NotificationsSettings
from app.core.events import Event
from app.mail.models import (
    Folder,
    FolderRole,
    Mailbox,
    MailboxAssignment,
    MailboxType,
    Message,
)
from app.mail.providers.base import Flag
from app.notifications import service
from app.notifications.models import MailNotification
from app.triage.models import TriageSource
from app.triage.service import Decision, save_result
from tests.factories import make_user
from tests.notifications.conftest import builtin_category
from tests.triage.conftest import NOW, Account, make_account

pytestmark = pytest.mark.db

SETTINGS = NotificationsSettings()
# Shortly after the test messages arrived (``Account.message``: NOW + n minutes).
LATER = NOW + timedelta(minutes=10)

Published = list[tuple[uuid.UUID, Event]]


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> Published:
    events: Published = []

    async def record(_session: object, user_id: uuid.UUID, event: Event) -> None:
        events.append((user_id, event))

    monkeypatch.setattr(service, "publish", record)
    return events


async def _triaged(
    account: Account,
    key: str = "important",
    *,
    source: TriageSource = TriageSource.LLM,
    **message: object,
) -> Message:
    mail = await account.message(**message)  # type: ignore[arg-type]
    category_id = await builtin_category(account.session, key)
    await save_result(account.session, mail.id, Decision(category_id, 1, source))
    return mail


async def _opt_in(session: AsyncSession, user_id: uuid.UUID, *keys: str) -> None:
    await service.save_settings(
        session,
        user_id,
        enabled=True,
        category_ids=[await builtin_category(session, key) for key in keys],
    )


async def _notify(account: Account, mail: Message, **settings: object) -> list[uuid.UUID]:
    return await service.notify_triaged(
        account.session,
        mail.id,
        account.mailbox.id,
        NotificationsSettings(**settings) if settings else SETTINGS,  # type: ignore[arg-type]
        now=LATER,
    )


def _event(mail: Message) -> Event:
    return Event(
        type="notification.message",
        ids={"message_id": mail.id, "mailbox_id": mail.mailbox_id},
    )


async def test_settings_default_to_off(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    stored = await service.user_settings(db_session, user.id)
    assert (stored.enabled, stored.category_ids, stored.show_subject, stored.sound) == (
        False,
        [],
        False,
        False,
    )


async def test_opted_in_owner_is_notified_once(
    db_session: AsyncSession, published: Published
) -> None:
    account = await make_account(db_session)
    await _opt_in(db_session, account.user.id, "important", "action_required")
    mail = await _triaged(account, "important")

    assert await _notify(account, mail) == [account.user.id]
    # Re-triage, retries and reprocessing do not announce it again.
    assert await _notify(account, mail) == []
    assert published == [(account.user.id, _event(mail))]
    stored = await db_session.scalar(
        select(MailNotification).where(MailNotification.message_id == mail.id)
    )
    assert stored is not None


async def test_nobody_is_notified_without_opt_in(
    db_session: AsyncSession, published: Published
) -> None:
    account = await make_account(db_session)
    mail = await _triaged(account, "important")
    assert await _notify(account, mail) == []
    # Opted in to other categories, or switched off again.
    await _opt_in(db_session, account.user.id, "action_required")
    assert await _notify(account, mail) == []
    await _opt_in(db_session, account.user.id, "important")
    await service.save_settings(db_session, account.user.id, enabled=False)
    assert await _notify(account, mail) == []
    assert published == []


@pytest.mark.parametrize(
    "case",
    ["seen", "old", "not_in_inbox", "corrected", "admin_off"],
)
async def test_only_new_unread_inbox_mail_classified_by_the_pipeline(
    db_session: AsyncSession, published: Published, case: str
) -> None:
    account = await make_account(db_session)
    await _opt_in(db_session, account.user.id, "important")
    mail = await _triaged(
        account,
        "important",
        source=TriageSource.USER if case == "corrected" else TriageSource.LLM,
        in_inbox=case != "not_in_inbox",
    )
    if case == "seen":
        mail.flags = [Flag.SEEN.value]
        await db_session.flush()
    settings: dict[str, object] = {"enabled": case != "admin_off"}
    if case == "old":
        # An initial import or a backlog: received more than ``max_age_minutes`` ago.
        settings["max_age_minutes"] = 5
    assert await _notify(account, mail, **settings) == []
    assert published == []


async def test_shared_mailbox_notifies_each_reader_who_opted_in(
    db_session: AsyncSession, published: Published
) -> None:
    account = await make_account(db_session)
    team = Mailbox(
        type=MailboxType.IMAP,
        display_name="Support",
        address="support@example.org",
        owner_user_id=None,
        is_shared=True,
    )
    db_session.add(team)
    await db_session.flush()
    inbox = Folder(mailbox_id=team.id, remote_id="INBOX", name="INBOX", role=FolderRole.INBOX)
    db_session.add(inbox)
    shared = Account(db_session, account.user, team, inbox)
    erika, max_ = await make_user(db_session), await make_user(db_session)
    db_session.add_all(
        [
            MailboxAssignment(mailbox_id=team.id, user_id=erika.id),
            MailboxAssignment(mailbox_id=team.id, user_id=max_.id),
        ]
    )
    await db_session.flush()
    await _opt_in(db_session, erika.id, "action_required")
    await _opt_in(db_session, max_.id, "important")
    # The owner of another mailbox never hears about the shared one.
    await _opt_in(db_session, account.user.id, "action_required")
    mail = await _triaged(shared, "action_required")

    assert await _notify(shared, mail) == [erika.id]
    assert published == [(erika.id, _event(mail))]


async def test_unknown_categories_are_rejected(db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    with pytest.raises(service.UnknownCategoryError):
        await service.save_settings(db_session, user.id, category_ids=[uuid.uuid4()])


async def test_content_hides_the_subject_unless_chosen(db_session: AsyncSession) -> None:
    account = await make_account(db_session)
    mail = await _triaged(account, "important", subject="Contract renewal", sender_name="Erika")
    content = await service.notification_content(db_session, account.user.id, mail.id)
    assert content is not None
    assert (content.sender, content.category_builtin_key, content.subject, content.sound) == (
        "Erika",
        "important",
        None,
        False,
    )
    await service.save_settings(db_session, account.user.id, show_subject=True, sound=True)
    content = await service.notification_content(db_session, account.user.id, mail.id)
    assert content is not None
    assert (content.subject, content.sound) == ("Contract renewal", True)


async def test_content_falls_back_to_the_address(db_session: AsyncSession) -> None:
    account = await make_account(db_session)
    mail = await _triaged(account, "important", sender_name=None, sender="noreply@example.org")
    content = await service.notification_content(db_session, account.user.id, mail.id)
    assert content is not None and content.sender == "noreply@example.org"


async def test_content_only_for_readers(db_session: AsyncSession) -> None:
    account = await make_account(db_session)
    other = await make_account(db_session)
    mail = await _triaged(account, "important")
    assert await service.notification_content(db_session, other.user.id, mail.id) is None

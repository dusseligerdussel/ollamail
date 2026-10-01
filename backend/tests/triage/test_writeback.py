"""Write-back of the category to the server via ``MailProvider`` (fake provider)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.models import Folder, Message
from app.mail.providers.base import MailboxConfig, RemoteFolder
from app.mail.providers.fake import FakeMailProvider
from app.triage.categories import effective_categories
from app.triage.models import TriageCategory, TriageResult, TriageSource, WriteBackMode
from app.triage.service import Decision, correct, save_result
from app.triage.writeback import (
    label_for,
    pending_by_mailbox,
    set_write_back_mode,
    write_back_messages,
)
from tests.triage.conftest import Account

pytestmark = pytest.mark.db

RAW = b"From: a@example.org\r\nSubject: Test\r\n\r\nHello\r\n"


class Server:
    """A fake server holding the account's messages; hands out the provider."""

    def __init__(self, labels: bool = False) -> None:
        self.provider = FakeMailProvider(labels=labels)
        self.provider.add_folder(RemoteFolder("INBOX", "INBOX"))
        self.configs: list[MailboxConfig] = []

    def factory(self, config: MailboxConfig) -> FakeMailProvider:
        self.configs.append(config)
        return self.provider

    def actions(self) -> list[tuple[str, tuple[object, ...]]]:
        return [(a.name, a.args) for a in self.provider.actions]


async def _triaged(account: Account, server: Server, key: str) -> tuple[Message, TriageResult]:
    message = await account.message()
    message.remote_ref = server.provider.add_message("INBOX", RAW)
    categories = await effective_categories(account.session, account.user.id)
    category_id = next(c.id for c in categories if c.key == key)
    result = await save_result(
        account.session, message.id, Decision(category_id, 2, TriageSource.LLM)
    )
    return message, result


async def _write_back(account: Account, server: Server, *messages: Message) -> int:
    return await write_back_messages(
        account.session,
        account.mailbox.id,
        [m.id for m in messages],
        prefix="ollamail/",
        provider_factory=server.factory,
    )


def test_label_uses_builtin_key_or_slug() -> None:
    assert label_for(TriageCategory(name="Info", builtin_key="info"), "x/") == "x/info"
    assert label_for(TriageCategory(name="Projekt Ä"), "") == "projekt_a"


async def test_off_by_default(db_session: AsyncSession, account: Account) -> None:
    server = Server()
    message, result = await _triaged(account, server, "newsletter")

    assert await _write_back(account, server, message) == 0
    assert server.actions() == []
    assert result.write_back_pending


async def test_label_mode_sets_and_replaces_the_keyword(
    db_session: AsyncSession, account: Account
) -> None:
    server = Server()
    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.LABEL)
    message, result = await _triaged(account, server, "newsletter")

    assert await _write_back(account, server, message) == 1
    assert server.actions() == [("apply_label", (message.remote_ref, "ollamail/newsletter"))]
    assert "ollamail/newsletter" in server.provider.messages[message.remote_ref].flags
    assert (result.remote_label, result.write_back_pending) == ("ollamail/newsletter", False)
    assert server.configs[0].mailbox_id == account.mailbox.id

    # Nothing pending: no server call.
    assert await _write_back(account, server, message) == 0
    assert len(server.actions()) == 1

    spam = next(
        c.id for c in await effective_categories(db_session, account.user.id) if c.key == "spam"
    )
    await correct(db_session, account.user.id, message.id, spam, 3)
    assert await _write_back(account, server, message) == 1

    assert server.actions()[1:] == [
        ("remove_label", (message.remote_ref, "ollamail/newsletter")),
        ("apply_label", (message.remote_ref, "ollamail/spam")),
    ]
    assert server.provider.messages[message.remote_ref].flags == {"ollamail/spam"}
    assert server.provider.closed


async def test_move_mode_moves_into_an_existing_folder(
    db_session: AsyncSession, account: Account
) -> None:
    server = Server()
    server.provider.add_folder(RemoteFolder("ollamail/newsletter", "ollamail/newsletter"))
    db_session.add(
        Folder(
            mailbox_id=account.mailbox.id,
            remote_id="ollamail/newsletter",
            name="ollamail/newsletter",
        )
    )
    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.MOVE)
    message, result = await _triaged(account, server, "newsletter")
    old_ref = message.remote_ref

    await _write_back(account, server, message)

    assert server.actions() == [("move", (old_ref, "ollamail/newsletter"))]
    # IMAP assigns a new reference when moving.
    assert message.remote_ref != old_ref
    assert server.provider.messages[message.remote_ref].folder_ids == ("ollamail/newsletter",)
    assert (result.remote_label, result.write_back_pending) == ("ollamail/newsletter", False)


async def test_move_mode_without_folder_does_nothing(
    db_session: AsyncSession, account: Account
) -> None:
    server = Server()
    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.MOVE)
    message, result = await _triaged(account, server, "info")

    await _write_back(account, server, message)

    assert server.actions() == []
    assert (result.remote_label, result.write_back_pending) == (None, False)


async def test_message_gone_on_server_is_skipped(
    db_session: AsyncSession, account: Account
) -> None:
    server = Server()
    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.LABEL)
    message, result = await _triaged(account, server, "info")
    server.provider.delete_message(message.remote_ref)

    assert await _write_back(account, server, message) == 1
    assert result.write_back_pending is False
    assert result.remote_label is None


async def test_enabling_marks_existing_results_and_lists_them(
    db_session: AsyncSession, account: Account, other_account: Account
) -> None:
    server = Server()
    message, result = await _triaged(account, server, "info")
    _, other_result = await _triaged(other_account, server, "info")
    result.write_back_pending = other_result.write_back_pending = False
    await db_session.flush()

    assert await pending_by_mailbox(db_session, 10) == {}

    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.LABEL)
    await db_session.refresh(result)
    await db_session.refresh(other_result)

    assert result.write_back_pending and not other_result.write_back_pending
    assert await pending_by_mailbox(db_session, 10) == {account.mailbox.id: [message.id]}

    await set_write_back_mode(db_session, account.mailbox.id, WriteBackMode.OFF)
    assert await pending_by_mailbox(db_session, 10) == {}

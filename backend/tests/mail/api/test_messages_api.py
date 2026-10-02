"""Integration tests: read API for mails (inbox list, thread, body, attachments,
read/unread) with sign-in and PostgreSQL. All names, addresses and contents are invented."""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import GmailSettings, GraphSettings, Settings
from app.mail.api import providers
from app.mail.api.messages import get_flag_writer
from app.mail.flags import write_flags
from app.mail.models import Folder, MailboxType, Message
from app.mail.providers.base import AuthenticationError, MailboxConfig
from app.mail.providers.registry import ProviderRegistry
from app.mail.storage import AttachmentStorage
from tests.mail.api.conftest import NOW, FakeServer, add_mailbox, run_sync

pytestmark = pytest.mark.db

# 1x1 transparent PNG.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da63f8ffff3f0005fe02fea7d6a4bb0000000049454e44ae426082"
)


def html_mail(*, subject: str = "Quarterly figures", references: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "Max Mustermann <max@example.com>"
    message["To"] = "erika@example.org"
    message["Subject"] = subject
    message["Date"] = format_datetime(NOW - timedelta(hours=1))
    message["Message-ID"] = make_msgid(domain="example.com")
    if references:
        message["In-Reply-To"] = references
        message["References"] = references
    message.set_content("Plain version of the figures.")
    message.add_alternative(
        "<p>Figures attached.</p>"
        '<img src="https://tracker.example.net/pixel.gif" alt="">'
        '<img src="cid:logo@example.com" alt="Logo">'
        "<script>alert(1)</script>",
        subtype="html",
    )
    html_part = message.get_payload()[1]
    html_part.add_related(PNG, maintype="image", subtype="png", cid="<logo@example.com>")
    message.add_attachment(
        b"%PDF-1.4 invented", maintype="application", subtype="pdf", filename="figures.pdf"
    )
    return message.as_bytes()


@pytest.fixture
def flag_writes() -> list[uuid.UUID]:
    return []


@pytest.fixture(autouse=True)
def _record_flag_writes(app: FastAPI, flag_writes: list[uuid.UUID]) -> Iterator[None]:
    async def record(message_id: uuid.UUID) -> None:
        flag_writes.append(message_id)

    app.dependency_overrides[get_flag_writer] = lambda: record
    yield


async def synced_mailbox(
    client: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> str:
    mailbox = await add_mailbox(client)
    await run_sync(db_session, str(mailbox["id"]), server, storage)
    return str(mailbox["id"])


async def inbox(client: AsyncClient, **params: object) -> dict[str, object]:
    response = await client.get("/messages", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def ids(page: dict[str, object]) -> list[str]:
    items = page["items"]
    assert isinstance(items, list)
    return [item["id"] for item in items]


# -- access -------------------------------------------------------------------------------


async def test_requires_sign_in(anonymous: AsyncClient) -> None:
    some_id = uuid.uuid4()
    assert (await anonymous.get("/messages")).status_code == 401
    assert (await anonymous.get(f"/messages/{some_id}/thread")).status_code == 401
    assert (await anonymous.get(f"/messages/{some_id}/body")).status_code == 401
    assert (await anonymous.patch(f"/messages/{some_id}", json={"seen": True})).status_code == 401
    assert (
        await anonymous.get(f"/messages/{some_id}/attachments/{uuid.uuid4()}")
    ).status_code == 401
    assert (await anonymous.get("/mailboxes/providers")).status_code == 401


async def test_other_users_messages_are_not_found(
    erika: AsyncClient,
    bob: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
    flag_writes: list[uuid.UUID],
) -> None:
    server.provider.add_message("INBOX", html_mail(), received_at=NOW)
    await synced_mailbox(erika, db_session, server, storage)
    page = await inbox(erika)
    message_id = ids(page)[0]
    detail = (await erika.get(f"/messages/{message_id}/thread")).json()
    attachment_id = detail["messages"][-1]["attachments"][0]["id"]

    assert (await inbox(bob))["total"] == 0
    assert (await bob.get(f"/messages/{message_id}/thread")).status_code == 404
    assert (await bob.get(f"/messages/{message_id}/body")).status_code == 404
    response = await bob.patch(f"/messages/{message_id}", json={"seen": True})
    assert response.status_code == 404
    response = await bob.get(f"/messages/{message_id}/attachments/{attachment_id}")
    assert response.status_code == 404
    assert flag_writes == []


# -- list ---------------------------------------------------------------------------------


async def test_inbox_lists_inbox_messages_newest_first(
    erika: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> None:
    mailbox_id = await synced_mailbox(erika, db_session, server, storage)
    page = await inbox(erika)
    # The fixture server has two messages in the inbox, one in the archive, one in trash.
    assert page["total"] == 2
    assert page["next_cursor"] is None
    items = page["items"]
    assert isinstance(items, list)
    first = items[0]
    assert set(first) == {
        "id",
        "mailbox_id",
        "thread_id",
        "subject",
        "sender",
        "snippet",
        "date",
        "unread",
        "flagged",
        "has_attachments",
    }
    assert first["mailbox_id"] == mailbox_id
    assert first["unread"] is True
    assert [item["date"] for item in items] == sorted(
        (item["date"] for item in items), reverse=True
    )

    archive = await db_session.scalar(select(Folder.id).where(Folder.remote_id == "Archive"))
    assert (await inbox(erika, folder_id=str(archive)))["total"] == 1
    assert (await inbox(erika, mailbox_id=mailbox_id))["total"] == 2
    assert (await inbox(erika, mailbox_id=str(uuid.uuid4())))["total"] == 0


async def test_paging_and_unread_filter(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    for index in range(5):
        server.provider.add_message(
            "INBOX",
            html_mail(subject=f"Report {index}"),
            received_at=NOW - timedelta(minutes=index),
            flags=frozenset({"seen"}) if index % 2 else frozenset(),
        )
    await synced_mailbox(erika, db_session, server, storage)

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, object] = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = await inbox(erika, **params)
        assert page["total"] == 7
        seen.extend(ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7

    assert (await inbox(erika, unread=True))["total"] == 5
    assert (await inbox(erika, unread=False))["total"] == 2
    response = await erika.get("/messages", params={"cursor": "not-a-cursor"})
    assert response.status_code == 422


# -- thread and body ----------------------------------------------------------------------


async def test_thread_contains_sanitised_html_without_external_images(
    erika: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> None:
    server.provider.add_message("INBOX", html_mail(), received_at=NOW + timedelta(hours=1))
    await synced_mailbox(erika, db_session, server, storage)
    message_id = ids(await inbox(erika))[0]

    response = await erika.get(f"/messages/{message_id}/thread")
    assert response.status_code == 200
    thread = response.json()
    assert thread["subject"] == "Quarterly figures"
    [message] = thread["messages"]
    assert message["sender"] == {"name": "Max Mustermann", "address": "max@example.com"}
    assert message["to"] == [{"name": None, "address": "erika@example.org"}]
    assert message["text"].startswith("Plain version")
    html = message["body"]["html"]
    assert "Figures attached." in html
    assert "tracker.example.net" not in html
    assert "<script" not in html
    assert message["body"]["blocked_images"] == 1
    # The cid: image points at the inline attachment.
    inline = next(a for a in message["attachments"] if a["content_type"] == "image/png")
    assert f"/api/messages/{message_id}/attachments/{inline['id']}?inline=true" in html.replace(
        "&amp;", "&"
    )
    names = [a["filename"] for a in message["attachments"] if not a["is_inline"]]
    assert names == ["figures.pdf"]

    body = (
        await erika.get(f"/messages/{message_id}/body", params={"external_images": True})
    ).json()
    assert "https://tracker.example.net/pixel.gif" in body["html"]
    assert body["blocked_images"] == 0


async def test_thread_groups_replies(
    erika: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> None:
    first = html_mail(subject="Planning")
    first_id = next(
        line.split(":", 1)[1].strip()
        for line in first.decode().splitlines()
        if line.startswith("Message-ID:")
    )
    server.provider.add_message("INBOX", first, received_at=NOW + timedelta(hours=1))
    server.provider.add_message(
        "INBOX",
        html_mail(subject="Re: Planning", references=first_id),
        received_at=NOW + timedelta(hours=2),
    )
    await synced_mailbox(erika, db_session, server, storage)
    newest = ids(await inbox(erika))[0]

    thread = (await erika.get(f"/messages/{newest}/thread")).json()
    assert [m["subject"] for m in thread["messages"]] == ["Planning", "Re: Planning"]
    assert thread["subject"] == "Planning"
    assert thread["thread_id"] is not None


# -- attachments --------------------------------------------------------------------------


async def test_attachment_download(
    erika: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> None:
    server.provider.add_message("INBOX", html_mail(), received_at=NOW + timedelta(hours=1))
    await synced_mailbox(erika, db_session, server, storage)
    message_id = ids(await inbox(erika))[0]
    [message] = (await erika.get(f"/messages/{message_id}/thread")).json()["messages"]
    by_type = {a["content_type"]: a["id"] for a in message["attachments"]}
    base = f"/messages/{message_id}/attachments"

    pdf = await erika.get(f"{base}/{by_type['application/pdf']}")
    assert pdf.status_code == 200
    assert pdf.content == b"%PDF-1.4 invented"
    assert pdf.headers["content-type"] == "application/octet-stream"
    assert pdf.headers["content-disposition"] == 'attachment; filename="figures.pdf"'
    assert pdf.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in pdf.headers["content-security-policy"]

    # Only raster images are shown inline.
    still_download = await erika.get(f"{base}/{by_type['application/pdf']}?inline=true")
    assert still_download.headers["content-disposition"].startswith("attachment")
    image = await erika.get(f"{base}/{by_type['image/png']}?inline=true")
    assert image.headers["content-type"] == "image/png"
    assert image.headers["content-disposition"].startswith("inline")
    assert image.content == PNG

    assert (await erika.get(f"{base}/{uuid.uuid4()}")).status_code == 404


async def test_missing_attachment_file_is_not_found(
    erika: AsyncClient, db_session: AsyncSession, server: FakeServer, storage: AttachmentStorage
) -> None:
    server.provider.add_message("INBOX", html_mail(), received_at=NOW + timedelta(hours=1))
    await synced_mailbox(erika, db_session, server, storage)
    message_id = ids(await inbox(erika))[0]
    [message] = (await erika.get(f"/messages/{message_id}/thread")).json()["messages"]
    attachment = message["attachments"][0]
    stored = await db_session.scalar(select(Message).where(Message.id == uuid.UUID(message_id)))
    assert stored is not None
    storage.delete_mailbox(stored.mailbox_id)
    response = await erika.get(f"/messages/{message_id}/attachments/{attachment['id']}")
    assert response.status_code == 404


# -- read / unread ------------------------------------------------------------------------


async def test_mark_read_and_unread(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
    flag_writes: list[uuid.UUID],
) -> None:
    await synced_mailbox(erika, db_session, server, storage)
    message_id = ids(await inbox(erika))[0]

    response = await erika.patch(f"/messages/{message_id}", json={"seen": True})
    assert response.status_code == 200
    assert response.json()["unread"] is False
    assert flag_writes == [uuid.UUID(message_id)]
    # No change, no write.
    await erika.patch(f"/messages/{message_id}", json={"seen": True})
    assert flag_writes == [uuid.UUID(message_id)]

    response = await erika.patch(f"/messages/{message_id}", json={"seen": False})
    assert response.json()["unread"] is True
    assert len(flag_writes) == 2
    assert (await inbox(erika, unread=True))["total"] == 2


async def test_write_flags_sets_server_flags(
    erika: AsyncClient,
    db_session: AsyncSession,
    server: FakeServer,
    storage: AttachmentStorage,
) -> None:
    await synced_mailbox(erika, db_session, server, storage)
    message_id = uuid.UUID(ids(await inbox(erika))[0])
    await erika.patch(f"/messages/{message_id}", json={"seen": True})
    message = await db_session.get(Message, message_id)
    assert message is not None

    assert await write_flags(db_session, message_id, provider_factory=server.factory)
    action = server.provider.actions[-1]
    assert action.name == "set_flags"
    assert action.args == (message.remote_ref, frozenset({"seen"}))

    def failing(config: MailboxConfig) -> FakeServer:
        raise AuthenticationError()

    assert not await write_flags(db_session, message_id, provider_factory=failing)  # type: ignore[arg-type]
    assert not await write_flags(db_session, uuid.uuid4(), provider_factory=server.factory)


# -- providers ----------------------------------------------------------------------------


async def test_providers_lists_form_types(erika: AsyncClient) -> None:
    response = await erika.get("/mailboxes/providers")
    assert response.status_code == 200
    assert response.json() == [{"type": "imap", "connect": "credentials", "oauth_start_path": None}]


def test_oauth_providers_only_when_configured(settings: Settings) -> None:
    registry = ProviderRegistry()
    for type in MailboxType:
        registry.register(type, lambda config: None)  # type: ignore[arg-type,return-value]
    types = [p.type for p in providers.available(settings, registry)]
    assert types == [MailboxType.IMAP]

    configured = settings.model_copy(
        update={
            "gmail": GmailSettings(
                client_id="client",
                client_secret=SecretStr("invented"),
                redirect_uri="http://localhost:8080/api/mail/gmail/oauth/callback",
            ),
            "graph": GraphSettings(client_id="client", client_secret=SecretStr("invented")),
        }
    )
    available = providers.available(configured, registry)
    assert [(p.type, p.connect, p.oauth_start_path) for p in available] == [
        (MailboxType.IMAP, "credentials", None),
        (MailboxType.GRAPH, "oauth", "/mail/graph/connect"),
        (MailboxType.GMAIL, "oauth", "/mail/gmail/oauth/start"),
    ]
    # A client ID without secret is not enough for the connect flow.
    no_secret = settings.model_copy(update={"graph": GraphSettings(client_id="client")})
    assert MailboxType.GRAPH not in [p.type for p in providers.available(no_secret, registry)]

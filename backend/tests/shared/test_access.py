"""Shared mailboxes (#34): one access rule for every feature, revoked at once.

A shared mailbox is assigned to Anna directly and to Ben through a group. Both see its
mails in every feature; after the admin removes the assignments, the very next request of
each of them shows nothing of it any more: inbox, thread, attachments, triage, todos,
search, RAG (new and stored answers) and digest. All names, addresses and texts are
invented (docs/PRIVACY.md).
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import get_llm
from app.audit.models import audit_events
from app.auth.models import Identity
from app.core.config import Settings, StorageSettings
from app.core.db import get_db
from app.digest import content as digest_content
from app.digest.models import Digest, DigestLength, DigestStatus, DigestTrigger
from app.mail.access import accessible_mailbox_ids, reader_ids
from app.mail.models import Attachment, Folder, FolderRole, Mailbox, MailboxAssignment
from app.main import create_app
from app.rag.router import get_session_factory
from app.search.service import index_message, search
from app.todos.models import Todo
from app.triage.categories import effective_categories
from app.triage.models import TriageResult, TriageSource
from app.users.models import User, UserRole
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.rag.conftest import FakeLLM, Inbox, session_factory
from tests.rag.test_api import ask

pytestmark = pytest.mark.db

SECRET = "The support hotline moves to the new office on Friday."
GROUP = "Support-Team"


@pytest.fixture
async def app(
    settings: Settings, db_session: AsyncSession, fake_llm: FakeLLM, tmp_path: Path
) -> AsyncIterator[FastAPI]:
    # Attachments live where the search fixtures' ``storage`` writes them.
    settings = settings.model_copy(
        update={"storage": StorageSettings.model_validate({"data_dir": tmp_path})}
    )
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_llm] = lambda: fake_llm.gateway
    app.dependency_overrides[get_session_factory] = lambda: session_factory(db_session)
    yield app
    await app.state.database.dispose()


async def client_for(
    app: FastAPI, session: AsyncSession, email: str, role: UserRole = UserRole.USER
) -> tuple[AsyncClient, User]:
    user = await make_local_user(session, email, role=role)
    client = api_client(app)
    await client.__aenter__()
    assert (await login(client, email)).status_code == 200
    return client, user


@dataclass
class Team:
    admin: AsyncClient
    anna: AsyncClient
    ben: AsyncClient
    anna_id: uuid.UUID
    ben_id: uuid.UUID
    admin_id: uuid.UUID
    mailbox_id: uuid.UUID
    message_id: uuid.UUID
    attachment_id: uuid.UUID
    todo_id: uuid.UUID
    digest_id: uuid.UUID


@pytest.fixture
async def team(app: FastAPI, db_session: AsyncSession, inbox: Inbox) -> AsyncIterator[Team]:
    admin, admin_user = await client_for(app, db_session, "admin@example.org", UserRole.ADMIN)
    anna, anna_user = await client_for(app, db_session, "anna@example.org")
    ben, ben_user = await client_for(app, db_session, "ben@example.org")
    # Ben's identity reported the group at its last login (in other case).
    identity = await db_session.scalar(select(Identity).where(Identity.user_id == ben_user.id))
    assert identity is not None
    identity.groups = ["support-team"]

    mailbox_id = await inbox.mail.mailbox(None)
    folder = Folder(mailbox_id=mailbox_id, remote_id="INBOX", name="Inbox", role=FolderRole.INBOX)
    db_session.add(folder)
    await db_session.flush()
    message_id = await inbox.mail.message(
        mailbox_id,
        SECRET,
        subject="Hotline move",
        sent_at=datetime(2026, 10, 1, 10, tzinfo=UTC),
        folders=[folder],
        attachments=[("plan.txt", "text/plain", b"Floor plan of the new office")],
    )
    await index_message(
        db_session,
        message_id,
        embedder=inbox.embedder,
        storage=inbox.storage,
        settings=inbox.settings,
    )
    (category, *_) = await effective_categories(db_session, None)
    db_session.add(
        TriageResult(
            message_id=message_id,
            category_id=category.id,
            priority=1,
            source=TriageSource.LLM,
            reason="The team has to tell callers about the move.",
        )
    )
    todo = Todo(mailbox_id=mailbox_id, message_id=message_id, title="Update the hotline notice")
    digest = Digest(
        user_id=anna_user.id,
        trigger=DigestTrigger.MANUAL,
        status=DigestStatus.READY,
        period_start=datetime(2026, 10, 1, tzinfo=UTC),
        period_end=datetime(2026, 10, 2, tzinfo=UTC),
        language="en",
        length=DigestLength.NORMAL,
        title="Digest",
        script="# Digest\n\nThe hotline moves on Friday [1].\n",
        mailbox_ids=[mailbox_id],
    )
    db_session.add_all([todo, digest])
    await db_session.flush()
    attachment_id = (
        await db_session.scalars(select(Attachment.id).where(Attachment.message_id == message_id))
    ).one()

    response = await admin.put(
        f"/admin/shared-mailboxes/{mailbox_id}/assignments",
        json={"users": [str(anna_user.id)], "groups": [{"group": GROUP}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reader_count"] == 2

    yield Team(
        admin=admin,
        anna=anna,
        ben=ben,
        anna_id=anna_user.id,
        ben_id=ben_user.id,
        admin_id=admin_user.id,
        mailbox_id=mailbox_id,
        message_id=message_id,
        attachment_id=attachment_id,
        todo_id=todo.id,
        digest_id=digest.id,
    )
    for client in (admin, anna, ben):
        await client.__aexit__(None, None, None)


async def visible_contents(
    client: AsyncClient, user_id: uuid.UUID, team: Team, inbox: Inbox, fake_llm: FakeLLM
) -> dict[str, Any]:
    """What ``client`` sees of the shared mailbox, feature by feature."""
    session = inbox.mail.session
    message = f"/messages/{team.message_id}"
    triage_inbox = (await client.get("/triage/inbox")).json()
    hits = await search(session, user_id, "hotline office", embedder=None, settings=inbox.settings)
    fake_llm.model.analysis = {"search_query": "hotline office"}
    fake_llm.model.answer = "On Friday [1]."
    events = await ask(client, question="When does the hotline move?")
    collected = await digest_content.collect(
        session,
        user_id,
        [team.mailbox_id],
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 11, 1, tzinfo=UTC),
        today=datetime(2026, 10, 2).date(),
        settings=digest_content.DigestSettings(),
    )
    return {
        "mailbox": str(team.mailbox_id)
        in [m["id"] for m in (await client.get("/mailboxes")).json()],
        "inbox": (await client.get("/messages")).json()["total"],
        "thread": (await client.get(f"{message}/thread")).status_code,
        "body": (await client.get(f"{message}/body")).status_code,
        "attachment": (await client.get(f"{message}/attachments/{team.attachment_id}")).status_code,
        "triage": (await client.get(f"/triage/messages/{team.message_id}")).status_code,
        "triage_inbox": sum(len(group["messages"]) for group in triage_inbox),
        "todos": [t["id"] for t in (await client.get("/todos")).json()],
        "todo": (await client.get(f"/todos/{team.todo_id}")).status_code,
        "search": len(hits),
        "search_api": len(
            (await client.post("/search", json={"query": "hotline office"})).json()["hits"]
        ),
        "rag_sources": len(events[2][1]["sources"]),
        "digest_mails": len(collected.mails),
        "members": (await client.get(f"/mailboxes/{team.mailbox_id}/members")).status_code,
    }


VISIBLE = {
    "mailbox": True,
    "inbox": 1,
    "thread": 200,
    "body": 200,
    "attachment": 200,
    "triage": 200,
    "triage_inbox": 1,
    "todo": 200,
    # Mail body and attachment.
    "search": 2,
    # One hit per message.
    "search_api": 1,
    "rag_sources": 2,
    "digest_mails": 1,
    "members": 200,
}
GONE = {
    "mailbox": False,
    "inbox": 0,
    "thread": 404,
    "body": 404,
    "attachment": 404,
    "triage": 404,
    "triage_inbox": 0,
    "todos": [],
    "todo": 404,
    "search": 0,
    "search_api": 0,
    "rag_sources": 0,
    "digest_mails": 0,
    "members": 404,
}


async def test_revoking_access_hides_everything_at_once(
    team: Team, inbox: Inbox, fake_llm: FakeLLM, db_session: AsyncSession
) -> None:
    # Anna asks before the revocation; her stored answer cites the shared mailbox.
    for client, user_id in ((team.anna, team.anna_id), (team.ben, team.ben_id)):
        seen = await visible_contents(client, user_id, team, inbox, fake_llm)
        assert seen.pop("todos") == [str(team.todo_id)]
        assert seen == VISIBLE
    conversation = (await team.anna.get("/rag/conversations")).json()[0]["id"]
    stored = (await team.anna.get(f"/rag/conversations/{conversation}")).json()
    assert stored["messages"][1]["content"] == "On Friday [1]."
    assert [d["id"] for d in (await team.anna.get("/digests")).json()] == [str(team.digest_id)]

    response = await team.admin.put(
        f"/admin/shared-mailboxes/{team.mailbox_id}/assignments", json={"users": [], "groups": []}
    )
    assert response.status_code == 200
    assert (response.json()["assignments"], response.json()["reader_count"]) == ([], 0)

    for client, user_id in ((team.anna, team.anna_id), (team.ben, team.ben_id)):
        seen = await visible_contents(client, user_id, team, inbox, fake_llm)
        assert seen == GONE
        assert SECRET not in str(seen)
    # Stored answers written from the mailbox are withheld, citations removed.
    stored = (await team.anna.get(f"/rag/conversations/{conversation}")).json()
    answer = stored["messages"][1]
    assert (answer["content"], answer["withheld"], answer["citations"]) == ("", True, [])
    # The digest made of the mailbox is gone, also from the podcast feed's query.
    assert (await team.anna.get("/digests")).json() == []
    assert (await team.anna.get(f"/digests/{team.digest_id}")).status_code == 404
    # Mark-as-read cannot be used to probe either.
    response = await team.anna.patch(f"/messages/{team.message_id}", json={"seen": True})
    assert response.status_code == 404

    # Granted and revoked access is in the audit log (IDs and codes only).
    rows = (
        await db_session.execute(
            select(audit_events.c.action, audit_events.c.details)
            .where(audit_events.c.target_id == str(team.mailbox_id))
            .order_by(audit_events.c.id)
        )
    ).all()
    actions = [(action, details["principal"]) for action, details in rows]
    assert sorted(actions) == sorted(
        [
            ("mailbox.shared", "user"),
            ("mailbox.shared", "group"),
            ("mailbox.unshared", "user"),
            ("mailbox.unshared", "group"),
        ]
    )
    assert any(details.get("user_id") == str(team.anna_id) for _, details in rows)
    assert any(details.get("group") == GROUP for _, details in rows)


async def test_admins_manage_but_do_not_read(team: Team, inbox: Inbox, fake_llm: FakeLLM) -> None:
    listed = (await team.admin.get("/admin/shared-mailboxes")).json()
    assert [m["id"] for m in listed] == [str(team.mailbox_id)]
    assert listed[0]["status"]["message_count"] == 1
    # Metadata only: no mails through the admin role.
    seen = await visible_contents(team.admin, team.admin_id, team, inbox, fake_llm)
    assert seen == GONE
    # Users cannot use the admin API.
    assert (await team.anna.get("/admin/shared-mailboxes")).status_code == 403
    response = await team.anna.put(
        f"/admin/shared-mailboxes/{team.mailbox_id}/assignments",
        json={"users": [str(team.anna_id)]},
    )
    assert response.status_code == 403


async def test_mailbox_is_synced_once_for_all_readers(team: Team, db_session: AsyncSession) -> None:
    # One mailbox row (one sync job, one copy of the mails) for every reader.
    assert set(await reader_ids(db_session, team.mailbox_id)) == {team.anna_id, team.ben_id}
    anna = (await team.anna.get("/messages")).json()["items"]
    ben = (await team.ben.get("/messages")).json()["items"]
    assert [m["id"] for m in anna] == [m["id"] for m in ben] == [str(team.message_id)]
    shared = list(await db_session.scalars(select(Mailbox.id).where(Mailbox.is_shared)))
    assert shared == [team.mailbox_id]


async def test_readers_have_read_only_access(team: Team, db_session: AsyncSession) -> None:
    mailbox = (await team.anna.get(f"/mailboxes/{team.mailbox_id}")).json()
    assert (mailbox["is_shared"], mailbox["permissions"], mailbox["provider_settings"]) == (
        True,
        ["read"],
        {},
    )
    for method, path, body in (
        ("patch", f"/mailboxes/{team.mailbox_id}", {"display_name": "Mine"}),
        ("delete", f"/mailboxes/{team.mailbox_id}", None),
        ("post", f"/mailboxes/{team.mailbox_id}/sync", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        response = await getattr(team.anna, method)(path, **kwargs)
        assert response.status_code == 404, (method, path)
    response = await team.anna.patch(f"/messages/{team.message_id}", json={"seen": True})
    assert (response.status_code, response.json()["error_code"]) == (403, "read_only")


async def test_team_todos_are_assignable_to_readers(team: Team, db_session: AsyncSession) -> None:
    members = (await team.anna.get(f"/mailboxes/{team.mailbox_id}/members")).json()
    assert {m["id"] for m in members} == {str(team.anna_id), str(team.ben_id)}

    response = await team.anna.patch(
        f"/todos/{team.todo_id}", json={"assignee_id": str(team.ben_id)}
    )
    assert response.status_code == 200
    assert (response.json()["shared"], response.json()["assignee_id"]) == (True, str(team.ben_id))
    # Ben sees the assignment; somebody without access cannot be assigned.
    assert (await team.ben.get(f"/todos/{team.todo_id}")).json()["assignee_id"] == str(team.ben_id)
    response = await team.anna.patch(
        f"/todos/{team.todo_id}", json={"assignee_id": str(team.admin_id)}
    )
    assert (response.status_code, response.json()["error_code"]) == (422, "invalid_assignee")

    # A todo created from a shared mail is a team todo, assigned to its creator.
    created = await team.ben.post(
        "/todos", json={"title": "Print the new address", "message_id": str(team.message_id)}
    )
    assert created.status_code == 201
    assert (created.json()["shared"], created.json()["assignee_id"]) == (True, str(team.ben_id))
    assert created.json()["id"] in [t["id"] for t in (await team.anna.get("/todos")).json()]
    # Own todos stay private and cannot be assigned.
    own = (await team.anna.post("/todos", json={"title": "Water the plants"})).json()
    assert own["id"] not in [t["id"] for t in (await team.ben.get("/todos")).json()]
    response = await team.anna.patch(f"/todos/{own['id']}", json={"assignee_id": str(team.ben_id)})
    assert (response.status_code, response.json()["error_code"]) == (422, "not_assignable")


async def test_corrections_in_shared_mailboxes_use_organisation_categories(
    team: Team, db_session: AsyncSession
) -> None:
    own = await team.anna.post("/triage/categories", json={"name": "Private projects"})
    assert own.status_code == 201, own.text
    response = await team.anna.put(
        f"/triage/messages/{team.message_id}", json={"category_id": own.json()["id"], "priority": 2}
    )
    assert response.status_code == 422
    organisation = [
        c
        for c in (await team.anna.get("/triage/categories")).json()
        if c["scope"] == "organization"
    ]
    response = await team.anna.put(
        f"/triage/messages/{team.message_id}",
        json={"category_id": organisation[-1]["id"], "priority": 3},
    )
    assert response.status_code == 200
    # The correction applies to the whole mailbox: Ben sees it as well.
    ben = (await team.ben.get(f"/triage/messages/{team.message_id}")).json()
    assert (ben["category_id"], ben["source"]) == (organisation[-1]["id"], "user")


async def test_deleting_a_user_keeps_the_shared_mailbox(
    team: Team, db_session: AsyncSession
) -> None:
    anna = await db_session.get(User, team.anna_id)
    await db_session.delete(anna)
    await db_session.flush()
    assert await db_session.get(Mailbox, team.mailbox_id) is not None
    assignments = list(
        await db_session.scalars(
            select(MailboxAssignment).where(MailboxAssignment.mailbox_id == team.mailbox_id)
        )
    )
    assert [(a.user_id, a.group_name) for a in assignments] == [(None, GROUP)]
    assert set(await reader_ids(db_session, team.mailbox_id)) == {team.ben_id}


async def test_group_assignments_match_case_insensitively_and_by_provider(
    team: Team, db_session: AsyncSession
) -> None:
    accessible = select(accessible_mailbox_ids(team.ben_id).subquery())
    assert team.mailbox_id in set(await db_session.scalars(accessible))

    response = await team.admin.put(
        f"/admin/shared-mailboxes/{team.mailbox_id}/assignments",
        json={"groups": [{"group": GROUP, "provider": "oidc:entra"}]},
    )
    assert response.status_code == 200
    # Ben's group comes from his local identity, not from Entra ID.
    assert team.mailbox_id not in set(await db_session.scalars(accessible))

    response = await team.admin.put(
        f"/admin/shared-mailboxes/{team.mailbox_id}/assignments",
        json={"groups": [{"group": "SUPPORT-TEAM", "provider": "local"}]},
    )
    assert response.json()["reader_count"] == 1
    assert team.mailbox_id in set(await db_session.scalars(accessible))


async def test_unknown_users_are_rejected(team: Team) -> None:
    response = await team.admin.put(
        f"/admin/shared-mailboxes/{team.mailbox_id}/assignments",
        json={"users": [str(uuid.uuid4())]},
    )
    assert (response.status_code, response.json()["error_code"]) == (422, "unknown_user")
    # Personal mailboxes are no shared mailboxes.
    personal = await team.anna.get("/mailboxes")
    assert personal.status_code == 200
    response = await team.admin.get(f"/admin/shared-mailboxes/{uuid.uuid4()}")
    assert response.status_code == 404

"""Triage API: integration tests with real sessions and PostgreSQL."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event
from app.mail import access as mail_access
from app.mail.providers.base import Flag
from app.triage.models import TriageFeedback, TriageMailboxSettings, TriageSource
from app.triage.service import Decision, save_result
from app.users.models import UserRole
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.triage.conftest import Account, account_for, make_account

pytestmark = pytest.mark.db


async def _signed_in(
    client: AsyncClient, session: AsyncSession, role: UserRole = UserRole.USER
) -> Account:
    user = await make_local_user(session, f"{role}-{uuid.uuid4().hex[:6]}@example.org", role=role)
    assert (await login(client, user.email)).status_code == 200
    return await account_for(session, user)


async def _categories(client: AsyncClient) -> dict[str, dict[str, object]]:
    response = await client.get("/triage/categories")
    assert response.status_code == 200
    return {str(c["builtin_key"] or c["name"]): c for c in response.json()}


async def test_requires_authentication(db_client: AsyncClient) -> None:
    for path in ("/triage/categories", "/triage/inbox", "/triage/sender-rules"):
        assert (await db_client.get(path)).status_code == 401


async def test_user_categories_lifecycle(db_client: AsyncClient, db_session: AsyncSession) -> None:
    await _signed_in(db_client, db_session)

    defaults = await db_client.get("/triage/categories")
    assert [c["builtin_key"] for c in defaults.json()] == [
        "important",
        "action_required",
        "waiting_for",
        "info",
        "newsletter",
        "notification",
        "spam",
    ]
    assert {c["scope"] for c in defaults.json()} == {"organization"}

    created = await db_client.post(
        "/triage/categories",
        json={"name": " Project Apollo ", "description": "Mails about Apollo"},
    )
    assert created.status_code == 201
    apollo = created.json()
    assert (apollo["name"], apollo["scope"], apollo["hidden"]) == (
        "Project Apollo",
        "user",
        False,
    )

    renamed = await db_client.patch(
        f"/triage/categories/{apollo['id']}", json={"name": "Apollo", "hidden": True}
    )
    assert renamed.status_code == 200
    assert (renamed.json()["name"], renamed.json()["hidden"]) == ("Apollo", True)

    spam = (await _categories(db_client))["spam"]
    assert (
        await db_client.patch(f"/triage/categories/{spam['id']}", json={"name": "x"})
    ).status_code == 403
    hidden = await db_client.patch(f"/triage/categories/{spam['id']}", json={"hidden": True})
    assert hidden.json()["hidden"] is True

    ids = [c["id"] for c in (await db_client.get("/triage/categories")).json()]
    reordered = await db_client.put(
        "/triage/categories/order", json={"category_ids": list(reversed(ids))}
    )
    assert [c["id"] for c in reordered.json()] == list(reversed(ids))
    bad_order = await db_client.put("/triage/categories/order", json={"category_ids": ids[:2]})
    assert bad_order.status_code == 422

    assert (await db_client.delete(f"/triage/categories/{spam['id']}")).status_code == 404
    assert (await db_client.delete(f"/triage/categories/{apollo['id']}")).status_code == 204
    assert "Apollo" not in await _categories(db_client)


async def test_last_visible_category_cannot_be_hidden(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    categories = list((await _categories(db_client)).values())

    for category in categories[:-1]:
        response = await db_client.patch(
            f"/triage/categories/{category['id']}", json={"hidden": True}
        )
        assert response.status_code == 200
    last = await db_client.patch(
        f"/triage/categories/{categories[-1]['id']}", json={"hidden": True}
    )

    assert last.status_code == 409


async def test_categories_of_other_users_are_invisible(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    other = await make_account(db_session)
    from app.triage.models import TriageCategory

    secret = TriageCategory(owner_user_id=other.user.id, name="Secret")
    db_session.add(secret)
    await db_session.flush()
    await _signed_in(db_client, db_session)

    assert "Secret" not in await _categories(db_client)
    response = await db_client.patch(f"/triage/categories/{secret.id}", json={"hidden": True})
    assert response.status_code == 404
    assert (await db_client.delete(f"/triage/categories/{secret.id}")).status_code == 404


async def test_organization_categories_are_admin_only(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signed_in(db_client, db_session)
    assert (await db_client.get("/triage/organization/categories")).status_code == 403
    assert (
        await db_client.post("/triage/organization/categories", json={"name": "X"})
    ).status_code == 403

    await _signed_in(db_client, db_session, UserRole.ADMIN)
    created = await db_client.post(
        "/triage/organization/categories",
        json={"name": "Invoices", "description": "Bills to pay", "position": 3},
    )
    assert created.status_code == 201
    listed = await db_client.get("/triage/organization/categories")
    assert "Invoices" in [c["name"] for c in listed.json()]

    info = next(c for c in listed.json() if c["builtin_key"] == "info")
    renamed = await db_client.patch(
        f"/triage/organization/categories/{info['id']}", json={"name": "FYI"}
    )
    assert (renamed.json()["name"], renamed.json()["builtin_key"]) == ("FYI", None)
    deleted = await db_client.delete(f"/triage/organization/categories/{created.json()['id']}")
    assert deleted.status_code == 204


async def test_get_and_correct_triage(db_client: AsyncClient, db_session: AsyncSession) -> None:
    account = await _signed_in(db_client, db_session)
    categories = await _categories(db_client)
    message = await account.message()
    path = f"/triage/messages/{message.id}"

    assert (await db_client.get(path)).status_code == 404
    await save_result(
        db_session,
        message.id,
        Decision(
            uuid.UUID(str(categories["info"]["id"])),
            2,
            TriageSource.LLM,
            reason="Only informs.",
            model="m",
            prompt_version="triage@1",
        ),
    )
    triage = await db_client.get(path)
    assert triage.status_code == 200
    assert triage.json()["reason"] == "Only informs."
    assert triage.json()["source"] == "llm"

    corrected = await db_client.put(
        path, json={"category_id": categories["action_required"]["id"], "priority": 1}
    )
    assert corrected.status_code == 200
    body = corrected.json()
    assert (body["category_id"], body["priority"], body["source"], body["reason"]) == (
        categories["action_required"]["id"],
        1,
        "user",
        None,
    )
    feedback = (await db_session.scalars(select(TriageFeedback))).all()
    assert [(f.user_id, f.message_id) for f in feedback] == [(account.user.id, message.id)]

    invalid = await db_client.put(path, json={"category_id": str(uuid.uuid4()), "priority": 1})
    assert invalid.status_code == 422
    out_of_range = await db_client.put(
        path, json={"category_id": categories["info"]["id"], "priority": 4}
    )
    assert out_of_range.status_code == 422


async def test_messages_of_other_users_are_not_found(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    other = await make_account(db_session)
    foreign = await other.message()
    await _signed_in(db_client, db_session)
    categories = await _categories(db_client)

    assert (await db_client.get(f"/triage/messages/{foreign.id}")).status_code == 404
    response = await db_client.put(
        f"/triage/messages/{foreign.id}",
        json={"category_id": categories["info"]["id"], "priority": 1},
    )
    assert response.status_code == 404
    settings = await db_client.get(f"/triage/mailboxes/{other.mailbox.id}/settings")
    assert settings.status_code == 404


async def test_inbox_grouped_by_category(db_client: AsyncClient, db_session: AsyncSession) -> None:
    account = await _signed_in(db_client, db_session)
    other = await make_account(db_session)
    categories = await _categories(db_client)
    info = uuid.UUID(str(categories["info"]["id"]))
    spam = uuid.UUID(str(categories["spam"]["id"]))

    low = await account.message("Low info")
    high = await account.message("High info")
    hidden = await account.message("Hidden spam")
    untriaged = await account.message("New mail")
    await account.message("Archived", in_inbox=False)
    foreign = await other.message("Foreign")
    for message, category, priority in [
        (low, info, 3),
        (high, info, 1),
        (hidden, spam, 3),
        (foreign, info, 1),
    ]:
        await save_result(db_session, message.id, Decision(category, priority, TriageSource.LLM))
    await db_client.patch(f"/triage/categories/{spam}", json={"hidden": True})

    statements: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if "FROM mail_messages" in statement:
            statements.append(statement)

    engine = db_session.bind.engine.sync_engine  # type: ignore[union-attr]
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await db_client.get("/triage/inbox")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    # Counts and messages of all groups: two queries, not two per category (#140).
    assert len(statements) == 2
    groups = {(g["category"]["builtin_key"] if g["category"] else None): g for g in response.json()}
    assert "spam" not in groups
    assert [m["subject"] for m in groups["info"]["messages"]] == ["High info", "Low info"]
    assert groups["info"]["total"] == 2
    # Hidden-category and untriaged messages; messages without priority come last.
    assert [m["subject"] for m in groups[None]["messages"]] == ["Hidden spam", "New mail"]
    assert groups[None]["messages"][1]["priority"] is None
    assert response.json()[-1]["category"] is None
    assert groups["important"]["messages"] == [] and groups["important"]["total"] == 0

    limited = await db_client.get(
        "/triage/inbox", params={"limit": 1, "mailbox_id": str(account.mailbox.id)}
    )
    info_group = next(
        g for g in limited.json() if g["category"] and g["category"]["builtin_key"] == "info"
    )
    assert (len(info_group["messages"]), info_group["total"]) == (1, 2)
    other_mailbox = await db_client.get(
        "/triage/inbox", params={"mailbox_id": str(other.mailbox.id)}
    )
    assert all(g["total"] == 0 for g in other_mailbox.json())
    assert str(untriaged.id) in response.text


async def test_sender_rules_and_suggestions(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    account = await _signed_in(db_client, db_session)
    categories = await _categories(db_client)
    newsletter = categories["newsletter"]["id"]
    for i in range(3):
        message = await account.message(f"Digest {i}", sender="digest@example.org")
        response = await db_client.put(
            f"/triage/messages/{message.id}", json={"category_id": newsletter, "priority": 3}
        )
        assert response.status_code == 200

    suggestions = await db_client.get("/triage/sender-rules/suggestions")
    assert suggestions.json() == [
        {"sender": "digest@example.org", "category_id": newsletter, "priority": 3, "corrections": 3}
    ]

    created = await db_client.post(
        "/triage/sender-rules", json={"sender": " Digest@Example.org", "category_id": newsletter}
    )
    assert created.status_code == 201
    assert (created.json()["sender"], created.json()["priority"]) == ("digest@example.org", 3)
    duplicate = await db_client.post(
        "/triage/sender-rules", json={"sender": "digest@example.org", "category_id": newsletter}
    )
    assert duplicate.status_code == 409
    assert (await db_client.get("/triage/sender-rules/suggestions")).json() == []
    domain = await db_client.post(
        "/triage/sender-rules",
        json={"sender": "@example.net", "category_id": newsletter, "priority": 2},
    )
    assert domain.status_code == 201
    for invalid in ("no-at-sign", "a@b", "a b@example.org"):
        response = await db_client.post(
            "/triage/sender-rules", json={"sender": invalid, "category_id": newsletter}
        )
        assert response.status_code == 422
    unknown = await db_client.post(
        "/triage/sender-rules", json={"sender": "x@example.org", "category_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404

    rules = (await db_client.get("/triage/sender-rules")).json()
    # Ordered by the database collation, which differs between installations.
    assert sorted(r["sender"] for r in rules) == ["@example.net", "digest@example.org"]
    assert (await db_client.delete(f"/triage/sender-rules/{rules[0]['id']}")).status_code == 204
    assert (await db_client.delete(f"/triage/sender-rules/{rules[0]['id']}")).status_code == 404


async def test_mailbox_write_back_settings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    account = await _signed_in(db_client, db_session)
    path = f"/triage/mailboxes/{account.mailbox.id}/settings"

    assert (await db_client.get(path)).json() == {"write_back": "off"}
    updated = await db_client.put(path, json={"write_back": "label"})
    assert updated.status_code == 200
    assert (await db_client.get(path)).json() == {"write_back": "label"}
    assert (await db_client.put(path, json={"write_back": "everything"})).status_code == 422
    row = await db_session.scalar(
        select(TriageMailboxSettings).where(TriageMailboxSettings.mailbox_id == account.mailbox.id)
    )
    assert row is not None and row.write_back == "label"


async def test_triage_of_several_messages(db_client: AsyncClient, db_session: AsyncSession) -> None:
    account = await _signed_in(db_client, db_session)
    other = await make_account(db_session)
    info = uuid.UUID(str((await _categories(db_client))["info"]["id"]))
    triaged = await account.message("Triaged")
    untriaged = await account.message("New mail")
    foreign = await other.message("Foreign")
    for message in (triaged, foreign):
        await save_result(db_session, message.id, Decision(info, 2, TriageSource.LLM))

    response = await db_client.get(
        "/triage/messages", params={"ids": [str(triaged.id), str(untriaged.id), str(foreign.id)]}
    )

    assert response.status_code == 200
    assert [(r["message_id"], r["category_id"]) for r in response.json()] == [
        (str(triaged.id), str(info))
    ]
    assert (await db_client.get("/triage/messages")).status_code == 422


async def test_inbox_messages_ordered_by_category(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    account = await _signed_in(db_client, db_session)
    other = await make_account(db_session)
    categories = await _categories(db_client)
    important = uuid.UUID(str(categories["important"]["id"]))
    info = uuid.UUID(str(categories["info"]["id"]))
    spam = uuid.UUID(str(categories["spam"]["id"]))

    low = await account.message("Low info")
    high = await account.message("High info")
    urgent = await account.message("Important")
    hidden = await account.message("Hidden spam")
    untriaged = await account.message("New mail")
    await account.message("Archived", in_inbox=False)
    foreign = await other.message("Foreign")
    for message, category, priority in [
        (low, info, 3),
        (high, info, 1),
        (urgent, important, 2),
        (hidden, spam, 3),
        (foreign, info, 1),
    ]:
        await save_result(db_session, message.id, Decision(category, priority, TriageSource.LLM))
    await db_client.patch(f"/triage/categories/{spam}", json={"hidden": True})
    untriaged.flags = [Flag.SEEN.value]
    await db_session.flush()

    response = await db_client.get("/triage/inbox/messages")

    assert response.status_code == 200
    page = response.json()
    assert [m["subject"] for m in page["items"]] == [
        "Important",
        "High info",
        "Low info",
        # Hidden category and untriaged: last, without priority last.
        "Hidden spam",
        "New mail",
    ]
    assert [m["category_id"] for m in page["items"]] == [
        str(important),
        str(info),
        str(info),
        None,
        None,
    ]
    assert page["items"][0]["priority"] == 2 and page["items"][-1]["priority"] is None
    assert (page["total"], page["next_cursor"]) == (5, None)
    counts = {g["category_id"]: g["total"] for g in page["groups"]}
    assert (counts[str(important)], counts[str(info)], counts[None]) == (1, 2, 2)
    assert str(spam) not in counts
    assert page["groups"][-1]["category_id"] is None

    # Keyset pages across the segments (category x priority); counts only on the first.
    subjects: list[str] = []
    params: dict[str, str | int] = {"limit": 2}
    while True:
        next_page = (await db_client.get("/triage/inbox/messages", params=params)).json()
        subjects += [m["subject"] for m in next_page["items"]]
        assert (next_page["total"] is None) == ("cursor" in params)
        assert (next_page["groups"] is None) == ("cursor" in params)
        if next_page["next_cursor"] is None:
            break
        params["cursor"] = next_page["next_cursor"]
    assert subjects == [m["subject"] for m in page["items"]]
    invalid = await db_client.get("/triage/inbox/messages", params={"cursor": "not-a-cursor"})
    assert invalid.status_code == 422

    only_info = (
        await db_client.get("/triage/inbox/messages", params={"category": str(info)})
    ).json()
    assert [m["subject"] for m in only_info["items"]] == ["High info", "Low info"]
    assert only_info["total"] == 2
    # The counts ignore the category filter.
    assert {g["category_id"]: g["total"] for g in only_info["groups"]} == counts

    uncategorised = (
        await db_client.get("/triage/inbox/messages", params={"category": "none"})
    ).json()
    assert [m["subject"] for m in uncategorised["items"]] == ["Hidden spam", "New mail"]

    unread = (await db_client.get("/triage/inbox/messages", params={"unread": True})).json()
    assert "New mail" not in [m["subject"] for m in unread["items"]]
    assert {g["category_id"]: g["total"] for g in unread["groups"]}[None] == 1

    assert (
        await db_client.get("/triage/inbox/messages", params={"category": str(spam)})
    ).status_code == 422
    assert (
        await db_client.get("/triage/inbox/messages", params={"category": "other"})
    ).status_code == 422
    foreign_mailbox = (
        await db_client.get("/triage/inbox/messages", params={"mailbox_id": str(other.mailbox.id)})
    ).json()
    assert foreign_mailbox["total"] == 0


async def test_correction_notifies_the_ui(
    db_client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await _signed_in(db_client, db_session)
    info = (await _categories(db_client))["info"]["id"]
    message = await account.message()
    published: list[tuple[uuid.UUID, Event]] = []

    async def record(_: object, user_id: uuid.UUID, event: Event) -> None:
        published.append((user_id, event))

    # Events reach the mailbox's readers through the central access module.
    monkeypatch.setattr(mail_access, "publish", record)

    response = await db_client.put(
        f"/triage/messages/{message.id}", json={"category_id": info, "priority": 1}
    )

    assert response.status_code == 200
    assert published == [
        (
            account.user.id,
            Event(
                type="message.triaged",
                ids={"message_id": message.id, "mailbox_id": account.mailbox.id},
            ),
        )
    ]

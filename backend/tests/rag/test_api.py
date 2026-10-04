"""Integration tests: RAG API with sign-in, PostgreSQL and a fake chat model."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm import LLMUnavailableError, get_llm
from app.ai.llm.user_limits import UserLLMLimiter
from app.core.config import Settings
from app.core.db import get_db
from app.main import create_app
from app.rag.router import get_session_factory
from app.users.models import User
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client
from tests.rag.conftest import FakeLLM, Inbox, session_factory

pytestmark = pytest.mark.db

FLIGHT = "Your flight to Lisbon boards at 09:40 at gate B12."


@pytest.fixture
async def http(
    settings: Settings, db_session: AsyncSession, fake_llm: FakeLLM
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_llm] = lambda: fake_llm.gateway
    app.dependency_overrides[get_session_factory] = lambda: session_factory(db_session)
    async with api_client(app) as client:
        yield client
    await app.state.database.dispose()


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def signed_in(http: AsyncClient, session: AsyncSession, email: str) -> User:
    user = await make_local_user(session, email)
    assert (await login(http, email)).status_code == 200
    return user


async def ask(http: AsyncClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    response = await http.post("/rag/ask", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


async def test_requires_sign_in(http: AsyncClient) -> None:
    assert (await http.post("/rag/ask", json={"question": "x"})).status_code == 401
    assert (await http.get("/rag/conversations")).status_code == 401
    assert (await http.delete("/rag/conversations")).status_code == 401


async def test_ask_streams_answer_with_citations_and_stores_it(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    mailbox = await inbox.mail.mailbox(user.id)
    message_id = await inbox.add(mailbox, FLIGHT, subject="Booking")
    fake_llm.model.analysis = {"search_query": "flight gate"}
    fake_llm.model.answer = "Gate B12 [1], boarding 09:40 [1][2]."

    events = await ask(http, question="Which gate is my flight?")

    names = [name for name, _ in events]
    assert names[:3] == ["start", "filters", "sources"]
    assert set(names[3:-1]) == {"token"}
    assert names[-1] == "done"
    assert all(data["type"] == name for name, data in events)
    sources = events[2][1]["sources"]
    assert [(s["number"], s["message_id"]) for s in sources] == [(1, str(message_id))]
    assert "Subject: Booking" in sources[0]["heading"]
    text = "".join(data["text"] for name, data in events if name == "token")
    assert text == "Gate B12 [1], boarding 09:40 [1]."
    done = events[-1][1]
    assert (done["status"], done["citations"]) == ("answered", [1])
    assert isinstance(done["ttft_ms"], int)

    conversation_id = events[0][1]["conversation_id"]
    listed = (await http.get("/rag/conversations")).json()
    assert [c["id"] for c in listed] == [conversation_id]
    assert listed[0]["title"] == "Which gate is my flight?"
    stored = (await http.get(f"/rag/conversations/{conversation_id}")).json()
    question, answer = stored["messages"]
    assert question["filters"]["extracted"] == []
    assert answer["content"] == text
    assert [(c["number"], c["message_id"]) for c in answer["citations"]] == [(1, str(message_id))]

    # Follow-up in the same conversation.
    fake_llm.model.answer = "At 09:40 [1]."
    follow_up = await ask(http, question="And when?", conversation_id=conversation_id)
    assert follow_up[0][1]["conversation_id"] == conversation_id
    stored = (await http.get(f"/rag/conversations/{conversation_id}")).json()
    assert len(stored["messages"]) == 4


async def test_users_are_isolated(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    bob = await signed_in(http, db_session, "bob@example.org")
    bob_box = await inbox.mail.mailbox(bob.id)
    await inbox.add(bob_box, "Bob's secret: the merger closes on Monday.")
    fake_llm.model.analysis = {"search_query": "merger"}
    fake_llm.model.answer = "Monday [1]."
    bob_events = await ask(http, question="When does the merger close?")
    bob_conversation = bob_events[0][1]["conversation_id"]
    assert len(bob_events[2][1]["sources"]) == 1

    await signed_in(http, db_session, "erika@example.org")
    events = await ask(
        http,
        question="When does the merger close?",
        filters={"mailbox_ids": [str(bob_box)]},
    )
    assert events[2][1]["sources"] == []
    assert events[-1][1]["status"] == "no_evidence"
    assert "Monday" not in "".join(d.get("text", "") for _, d in events)

    # Bob's conversation behaves like a missing one.
    assert (await http.get(f"/rag/conversations/{bob_conversation}")).status_code == 404
    assert (await http.delete(f"/rag/conversations/{bob_conversation}")).status_code == 404
    response = await http.post(
        "/rag/ask", json={"question": "and?", "conversation_id": bob_conversation}
    )
    assert response.status_code == 404
    assert bob_conversation not in [c["id"] for c in (await http.get("/rag/conversations")).json()]
    # Deleting all own conversations leaves Bob's alone.
    assert (await http.delete("/rag/conversations")).status_code == 204
    assert (await http.get("/rag/conversations")).json() == []

    await login(http, "bob@example.org")
    assert (await http.get(f"/rag/conversations/{bob_conversation}")).status_code == 200


async def test_delete_conversation(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    await inbox.add(await inbox.mail.mailbox(user.id), FLIGHT)
    first = (await ask(http, question="flight"))[0][1]["conversation_id"]
    second = (await ask(http, question="gate"))[0][1]["conversation_id"]

    assert (await http.delete(f"/rag/conversations/{first}")).status_code == 204

    assert [c["id"] for c in (await http.get("/rag/conversations")).json()] == [second]
    assert (await http.get(f"/rag/conversations/{first}")).status_code == 404
    assert (await http.get(f"/rag/conversations/{uuid.uuid4()}")).status_code == 404


async def test_llm_outage_is_reported_in_the_stream(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    await inbox.add(await inbox.mail.mailbox(user.id), FLIGHT)
    fake_llm.model.analysis = LLMUnavailableError("down")
    fake_llm.model.answer = LLMUnavailableError("down")

    events = await ask(http, question="flight")

    assert events[-1] == ("error", {"type": "error", "code": "llm_unavailable"})
    assert (await http.get("/rag/conversations")).json() == []


async def test_llm_timeout_is_reported_in_the_stream(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    await inbox.add(await inbox.mail.mailbox(user.id), FLIGHT)
    fake_llm.hang_until_deadline()

    events = await ask(http, question="flight")

    assert events[-1] == ("error", {"type": "error", "code": "llm_timeout"})
    assert (await http.get("/rag/conversations")).json() == []


async def test_question_is_validated(http: AsyncClient, db_session: AsyncSession) -> None:
    await signed_in(http, db_session, "erika@example.org")

    assert (await http.post("/rag/ask", json={"question": "   "})).status_code == 422
    assert (await http.post("/rag/ask", json={"question": "x" * 2001})).status_code == 422
    response = await http.post("/rag/ask", json={"question": "x", "filters": {"owner": "bob"}})
    assert response.status_code == 422


async def test_parallel_asks_of_one_user_are_limited(
    http: AsyncClient, db_session: AsyncSession, inbox: Inbox, fake_llm: FakeLLM
) -> None:
    user = await signed_in(http, db_session, "erika@example.org")
    await inbox.add(await inbox.mail.mailbox(user.id), FLIGHT)
    limiter: UserLLMLimiter = http._transport.app.state.user_llm_limiter  # type: ignore[attr-defined]
    held = [limiter.acquire(user.id) for _ in range(limiter.limit)]

    response = await http.post("/rag/ask", json={"question": "Which gate?"})

    assert response.status_code == 429
    assert response.headers["retry-after"].isdigit()
    assert response.json()["error_code"] == "llm_busy"
    assert fake_llm.model.calls == []

    for slot in held:
        slot.release()
    await ask(http, question="Which gate?")
    # The slot of the streamed answer is free again.
    assert limiter.active(user.id) == 0

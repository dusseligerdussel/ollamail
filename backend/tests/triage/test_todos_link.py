"""The todo step reads the triage category to skip bulk mail."""

from collections.abc import Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import TodosSettings
from app.todos import extraction
from app.todos.extraction import extract_todos, message_category
from app.triage.categories import effective_categories
from app.triage.models import TriageCategory, TriageSource
from app.triage.service import Decision, category_key, save_result
from app.triage.tasks import install_category_lookup
from tests.triage.conftest import Account, FakeLLM

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _lookup() -> Iterator[None]:
    install_category_lookup()
    yield
    # The worker installs it on import; tests leave the default behind.
    extraction.set_category_lookup(None)


async def _triage(account: Account, key: str | None = None, name: str | None = None) -> object:
    session = account.session
    message = await account.message()
    if name is not None:
        own = TriageCategory(owner_user_id=account.user.id, name=name)
        session.add(own)
        await session.flush()
        category_id = own.id
    else:
        categories = await effective_categories(session, account.user.id)
        category_id = next(c.id for c in categories if c.key == key)
    await save_result(session, message.id, Decision(category_id, 3, TriageSource.LLM))
    return message


async def test_category_key(db_session: AsyncSession, account: Account) -> None:
    untriaged = await account.message()
    newsletter = await _triage(account, "newsletter")
    own = await _triage(account, name="Projekt Übersicht")

    assert await category_key(db_session, untriaged.id) is None
    assert await category_key(db_session, newsletter.id) == "newsletter"  # type: ignore[attr-defined]
    assert await message_category(db_session, own.id) == "projekt_ubersicht"  # type: ignore[attr-defined]


async def test_todo_step_skips_newsletters(
    db_session: AsyncSession, account: Account, fake_llm: FakeLLM
) -> None:
    newsletter = await _triage(account, "newsletter")

    created = await extract_todos(
        db_session,
        newsletter.id,
        llm=fake_llm.gateway,
        settings=TodosSettings(),  # type: ignore[attr-defined]
    )

    assert created == []
    assert fake_llm.provider.calls == []

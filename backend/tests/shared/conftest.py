"""Fixtures for the shared mailbox tests: indexed mails and a fake chat model from the
search and RAG tests."""

from tests.rag.conftest import (  # noqa: F401 (fixtures)
    embedder,
    fake_llm,
    inbox,
    mail,
    search_settings,
    storage,
)

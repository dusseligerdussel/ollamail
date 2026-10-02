"""Fixtures for AI settings tests; sign-in helpers and scratch databases come from the
auth tests."""

from collections.abc import Iterator

import pytest

from app.core.config import Settings
from app.core.crypto import KeyRing, set_keyring
from tests.auth.conftest import _cheap_hashing, scratch_database  # noqa: F401


@pytest.fixture(autouse=True)
def _keyring(settings: Settings) -> Iterator[None]:
    """API keys are encrypted with the process-wide key ring (set up at start-up)."""
    set_keyring(KeyRing.from_settings(settings.security))
    yield
    set_keyring(None)

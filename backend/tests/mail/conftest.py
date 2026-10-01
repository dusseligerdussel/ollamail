"""Fixtures for mail tests. All fixture mails are synthetic (example.* domains)."""

from pathlib import Path

import pytest

from app.mail.storage import AttachmentStorage

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_names() -> list[str]:
    return sorted(path.name for path in FIXTURES.glob("*.eml"))


@pytest.fixture
def storage(tmp_path: Path) -> AttachmentStorage:
    return AttachmentStorage(tmp_path)

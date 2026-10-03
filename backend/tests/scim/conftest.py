"""Fixtures for SCIM tests: an app on the rolled-back test session, an admin client and
an IdP client with a bearer token. All names and addresses are invented."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, StorageSettings
from app.core.db import get_db
from app.main import create_app
from app.privacy.router import get_user_deletion_requester
from app.scim import tokens
from app.scim.models import ScimConfig
from app.users.models import User, UserRole
from tests.auth.conftest import _cheap_hashing, login, make_local_user  # noqa: F401
from tests.conftest import api_client

SCIM_JSON = "application/scim+json"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


@pytest.fixture
async def app(
    settings: Settings, db_session: AsyncSession, tmp_path: Any
) -> AsyncIterator[FastAPI]:
    settings = settings.model_copy(
        update={"storage": StorageSettings.model_validate({"data_dir": tmp_path})}
    )
    app = create_app(settings)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    # Queued user deletions (#177); the jobs do not run.
    app.state.user_deletion_requests = []

    async def record_user_deletion(user_id: uuid.UUID) -> bool:
        app.state.user_deletion_requests.append(user_id)
        return True

    app.dependency_overrides[get_user_deletion_requester] = lambda: record_user_deletion
    yield app
    await app.state.database.dispose()


@pytest.fixture
async def admin(db_session: AsyncSession) -> User:
    return await make_local_user(db_session, "admin@example.org", role=UserRole.ADMIN)


@pytest.fixture
async def admin_client(app: FastAPI, admin: User) -> AsyncIterator[AsyncClient]:
    async with api_client(app) as client:
        assert (await login(client, admin.email)).status_code == 200
        yield client


async def issue_token(session: AsyncSession, *, enabled: bool = True) -> str:
    config = await tokens.get_config_for_update(session)
    config.enabled = enabled
    issued = tokens.new_token("Entra ID")
    session.add(issued.token)
    await session.commit()
    return issued.secret


def idp_client(app: FastAPI, token: str | None) -> AsyncClient:
    """A client like an IdP: no cookies, no CSRF token, bearer token, SCIM media type."""
    headers = {"Content-Type": SCIM_JSON, "Accept": SCIM_JSON}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://test/scim/v2",
        headers=headers,
    )


@pytest.fixture
async def scim_token(db_session: AsyncSession, admin: User) -> str:
    return await issue_token(db_session)


@pytest.fixture
async def idp(app: FastAPI, scim_token: str) -> AsyncIterator[AsyncClient]:
    async with idp_client(app, scim_token) as client:
        yield client


def ok(response: Response, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    if status == 204:
        return None
    assert response.headers["content-type"].startswith(SCIM_JSON)
    return response.json()


def patch_body(*operations: dict[str, Any]) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": list(operations)}


async def scim_config(session: AsyncSession) -> ScimConfig:
    return await tokens.get_config_for_update(session)

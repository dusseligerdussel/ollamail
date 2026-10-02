"""Synthetic Microsoft Graph responses for the provider tests (``respx``).

The JSON follows the structure documented for Microsoft Graph v1.0 (delta query, JSON
batching, OAuth token endpoint); all IDs, addresses and contents are made up.
"""

import base64
import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import GraphSettings, MailSettings, SecuritySettings
from app.core.crypto import generate_key
from app.mail.models import MailboxType
from app.mail.providers.base import MailboxConfig, SaveCredentials
from app.mail.providers.graph import GraphProvider
from tests.mail.conftest import load_fixture

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
TENANT = "00000000-0000-4000-8000-0000000000aa"
TOKEN_URL = f"https://{LOGIN_HOST}/{TENANT}/oauth2/v2.0/token"
NOW = 1_790_000_000.0

INBOX = "AAMkAGI2-inbox"
ARCHIVE = "AAMkAGI2-archive"
TRASH = "AAMkAGI2-trash"
SECURITY = SecuritySettings.model_validate({"secret_key": generate_key()})


def graph_settings(**values: Any) -> GraphSettings:
    defaults: dict[str, Any] = {
        "client_id": "11111111-2222-4333-8444-555555555555",
        "client_secret": SecretStr("test-client-secret"),
        "tenant_id": TENANT,
    }
    return GraphSettings.model_validate({**defaults, **values})


def credentials(*, expires_at: float = NOW + 3600) -> dict[str, Any]:
    return {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": int(expires_at),
    }


class Sleeps(list[float]):
    async def __call__(self, seconds: float) -> None:
        self.append(seconds)


def make_provider(
    *,
    settings: dict[str, Any] | None = None,
    graph: GraphSettings | None = None,
    creds: dict[str, Any] | None = None,
    save: SaveCredentials | None = None,
    sleep: Callable[[float], Any] | None = None,
    address: str = "erika@example.com",
    mailbox_id: uuid.UUID | None = None,
    batch_size: int = 50,
) -> GraphProvider:
    config = MailboxConfig(
        mailbox_id=mailbox_id or uuid.uuid4(),
        type=MailboxType.GRAPH,
        address=address,
        settings=settings if settings is not None else {"auth": "delegated", "user": "me"},
        credentials=creds if creds is not None else credentials(),
        save_credentials=save,
    )
    return GraphProvider(
        config,
        settings=graph or graph_settings(),
        mail_settings=MailSettings(sync_batch_size=batch_size),
        security=SECURITY,
        sleep=sleep if sleep is not None else Sleeps(),
        clock=lambda: NOW,
    )


def mime(fixture: str = "01-plain-ascii.eml") -> bytes:
    return load_fixture(fixture)


def message(
    message_id: str,
    folder: str = INBOX,
    *,
    read: bool = False,
    flagged: bool = False,
    categories: list[str] | None = None,
    attachments: bool = False,
    received: str = "2026-09-30T08:15:00Z",
) -> dict[str, Any]:
    return {
        "@odata.etag": 'W/"CQAAABYAAAA"',
        "id": message_id,
        "parentFolderId": folder,
        "receivedDateTime": received,
        "isRead": read,
        "isDraft": False,
        "flag": {"flagStatus": "flagged" if flagged else "notFlagged"},
        "categories": categories or [],
        "conversationId": f"conv-{message_id}",
        "hasAttachments": attachments,
    }


def removed(message_id: str) -> dict[str, Any]:
    return {"id": message_id, "@removed": {"reason": "deleted"}}


def page(
    items: list[dict[str, Any]], *, next_link: str | None = None, delta: str | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"@odata.context": f"{GRAPH}/$metadata#Collection(message)"}
    body["value"] = items
    if next_link:
        body["@odata.nextLink"] = next_link
    if delta:
        body["@odata.deltaLink"] = delta
    return httpx.Response(200, json=body)


def delta_url(folder: str, token: str, kind: str = "deltatoken") -> str:
    return f"{GRAPH}/me/mailFolders/{folder}/messages/delta?$" + f"{kind}={token}"


def graph_error(status: int, code: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": code, "message": "Synthetic server text erika@example.com"}},
        headers=headers,
    )


def batch_handler(
    respond: Callable[[dict[str, Any]], dict[str, Any]],
) -> Callable[[httpx.Request], httpx.Response]:
    """``$batch`` side effect: ``respond(request)`` returns one response object."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"responses": [respond(item) for item in payload["requests"]]}
        )

    return handler


def mime_part(request_id: str, source: bytes) -> dict[str, Any]:
    return {
        "id": request_id,
        "status": 200,
        "headers": {"Content-Type": "text/plain"},
        "body": base64.b64encode(source).decode("ascii"),
    }


def token_response(access: str = "access-2", refresh: str | None = "refresh-2") -> httpx.Response:
    body: dict[str, Any] = {"token_type": "Bearer", "expires_in": 3599, "access_token": access}
    if refresh:
        body["refresh_token"] = refresh
    return httpx.Response(200, json=body)


def form(request: httpx.Request) -> dict[str, str]:
    return dict(httpx.QueryParams(request.content.decode()))

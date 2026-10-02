"""Microsoft 365: OAuth connect flow and change notifications
(docs/providers/microsoft365.md §3.1, §5.4).

* ``POST /mail/graph/connect`` (signed in): returns the Microsoft sign-in URL. ``state``,
  PKCE verifier, user ID and return path travel in a short-lived cookie, encrypted and
  authenticated with a key derived from ``OLLAMAIL_SECRET_KEY``; nothing is stored
  server-side, so any API instance can handle the callback.
* ``GET /mail/graph/callback``: browser navigation from Microsoft. Checks ``state`` and
  that the signed-in user started the flow, exchanges the code, creates the mailbox (or
  replaces the tokens of the existing one) and redirects to the return path with
  ``graph=connected`` or ``graph=error&reason=<code>``. The watcher picks up a new mailbox
  on its next refresh and queues the initial import.
* ``POST /mail/graph/notifications``: change notifications from Graph (only with
  ``OLLAMAIL_MAIL_GRAPH_NOTIFICATION_URL``). Exempt from CSRF; a notification is trusted
  only if its ``clientState`` verifies, and it only queues a sync of that mailbox.
"""

import asyncio
import base64
import binascii
import hmac
import json
import os
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Annotated
from urllib.parse import quote, urlencode

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, get_settings_from_app
from app.auth.keys import derive_key
from app.auth.sessions import SESSION_COOKIE, resolve_session
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.mail.models import Mailbox, MailboxType
from app.mail.providers.base import AuthenticationError, ProviderError
from app.mail.providers.graph_auth import (
    TokenSet,
    authorization_url,
    code_verifier,
    delegated_scopes,
    exchange_code,
)
from app.mail.providers.graph_client import GraphClient
from app.mail.providers.graph_webhook import verify_client_state

log = get_logger(__name__)

router = APIRouter(prefix="/mail/graph", tags=["mail"])

NOTIFICATIONS_PATH = "/mail/graph/notifications"
FLOW_COOKIE = "ollamail_graph_connect"
FLOW_LIFETIME_SECONDS = 600
_AAD = b"ollamail graph connect v1"
_NONCE_LEN = 12
_MAX_NOTIFICATION_BYTES = 1024 * 1024
_DEFAULT_RETURN = "/"

DbDep = Annotated[AsyncSession, Depends(get_db)]
RequestSync = Callable[[uuid.UUID], Awaitable[object]]


class GraphConnectRequest(BaseModel):
    # Connect a mailbox the user has Full Access to instead of the own one.
    shared_mailbox: str | None = Field(
        default=None, max_length=320, pattern=r"^[^@\s/?#]+@[^@\s/?#]+$"
    )
    # Relative path of the web UI to return to afterwards.
    return_to: str | None = Field(default=None, max_length=2048)


class GraphConnectResponse(BaseModel):
    authorization_url: str


@dataclass(frozen=True)
class ConnectFlow:
    state: str
    verifier: str
    user_id: str
    redirect_uri: str
    return_to: str
    shared_mailbox: str | None
    expires_at: int


def _key(settings: Settings) -> bytes:
    return derive_key(settings.security, "graph-connect")


def seal(settings: Settings, flow: ConnectFlow) -> str:
    nonce = os.urandom(_NONCE_LEN)
    data = AESGCM(_key(settings)).encrypt(nonce, json.dumps(asdict(flow)).encode(), _AAD)
    return base64.urlsafe_b64encode(nonce + data).rstrip(b"=").decode()


def unseal(
    settings: Settings, value: str | None, *, now: float | None = None
) -> ConnectFlow | None:
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        plain = AESGCM(_key(settings)).decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], _AAD)
        flow = ConnectFlow(**json.loads(plain))
    except (binascii.Error, ValueError, InvalidTag, TypeError):
        return None
    if flow.expires_at < (time.time() if now is None else now):
        return None
    return flow


def safe_return_to(value: str | None) -> str:
    """Only relative paths of this site (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return _DEFAULT_RETURN
    if any(ord(c) < 0x20 for c in value):
        return _DEFAULT_RETURN
    return value


def _with_query(path: str, params: dict[str, str]) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode(params)}"


def _graph(settings: Settings) -> None:
    if not settings.graph.enabled or settings.graph.client_secret is None:
        raise ProblemError(404, detail="Microsoft 365 is not configured.")


def _redirect_uri(request: Request, settings: Settings) -> str:
    return settings.graph.redirect_uri or str(request.url_for("graph_callback"))


@router.post("/connect")
async def connect_graph_mailbox(
    body: GraphConnectRequest,
    request: Request,
    response: Response,
    current: CurrentSessionDep,
) -> GraphConnectResponse:
    """Start connecting a Microsoft 365 mailbox; the client navigates to the returned URL."""
    settings = get_settings_from_app(request)
    _graph(settings)
    flow = ConnectFlow(
        state=secrets.token_urlsafe(32),
        verifier=code_verifier(),
        user_id=str(current.user_id),
        redirect_uri=_redirect_uri(request, settings),
        return_to=safe_return_to(body.return_to),
        shared_mailbox=body.shared_mailbox.lower() if body.shared_mailbox else None,
        expires_at=int(time.time()) + FLOW_LIFETIME_SECONDS,
    )
    url = authorization_url(
        settings.graph,
        redirect_uri=flow.redirect_uri,
        state=flow.state,
        verifier=flow.verifier,
        scopes=delegated_scopes(settings.graph, shared=flow.shared_mailbox is not None),
    )
    response.set_cookie(
        FLOW_COOKIE,
        seal(settings, flow),
        max_age=FLOW_LIFETIME_SECONDS,
        path="/",
        secure=settings.auth.cookie_secure,
        httponly=True,
        # Sent on the top-level redirect back from Microsoft.
        samesite="lax",
    )
    return GraphConnectResponse(authorization_url=url)


@dataclass(frozen=True)
class _Profile:
    address: str
    display_name: str
    user_id: str | None


async def _profile(settings: Settings, tokens: TokenSet, shared_mailbox: str | None) -> _Profile:
    async def token(force: bool) -> str:
        return tokens.access_token

    client = GraphClient(
        api_url=settings.graph.api_url,
        token=token,
        timeout=settings.graph.timeout,
        max_retries=settings.graph.max_retries,
    )
    try:
        if shared_mailbox is not None:
            # Needs Full Access on the mailbox (Mail.ReadWrite.Shared).
            await client.get_json(
                f"/users/{quote(shared_mailbox, safe='@')}/mailFolders/inbox",
                params={"$select": "id"},
            )
            return _Profile(address=shared_mailbox, display_name=shared_mailbox, user_id=None)
        me = await client.get_json(
            "/me", params={"$select": "id,mail,userPrincipalName,displayName"}
        )
    finally:
        await client.aclose()
    address = me.get("mail") or me.get("userPrincipalName")
    if not isinstance(address, str) or "@" not in address:
        raise ProviderError(code="invalid_response")
    name = me.get("displayName")
    user_id = me.get("id")
    return _Profile(
        address=address.lower(),
        display_name=name if isinstance(name, str) and name else address,
        user_id=user_id if isinstance(user_id, str) else None,
    )


async def save_connected_mailbox(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    address: str,
    display_name: str,
    provider_settings: dict[str, str],
    tokens: TokenSet,
) -> tuple[Mailbox, bool]:
    """Create the user's Graph mailbox, or replace the tokens of an existing one with the
    same address (reconnect). Returns the mailbox and whether it was created."""
    mailbox = await db.scalar(
        select(Mailbox).where(
            Mailbox.owner_user_id == user_id,
            Mailbox.type == MailboxType.GRAPH,
            func.lower(Mailbox.address) == address.lower(),
        )
    )
    created = mailbox is None
    if mailbox is None:
        mailbox = Mailbox(
            id=uuid7(),
            type=MailboxType.GRAPH,
            display_name=display_name[:255],
            address=address,
            owner_user_id=user_id,
            is_shared=False,
        )
        db.add(mailbox)
    mailbox.provider_settings = provider_settings
    mailbox.credentials = tokens.to_credentials()
    await db.commit()
    return mailbox, created


@router.get("/callback", name="graph_callback", include_in_schema=False)
async def graph_callback(
    request: Request,
    db: DbDep,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    settings = get_settings_from_app(request)
    flow = unseal(settings, request.cookies.get(FLOW_COOKIE))
    return_to = flow.return_to if flow else _DEFAULT_RETURN

    def done(params: dict[str, str]) -> RedirectResponse:
        response = RedirectResponse(_with_query(return_to, params), status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/")
        return response

    def fail(reason: str) -> RedirectResponse:
        log.info("graph_connect_failed", reason=reason)
        return done({"graph": "error", "reason": reason})

    if flow is None or not state or not hmac.compare_digest(state, flow.state):
        return fail("state_invalid")
    token = request.cookies.get(SESSION_COOKIE)
    session = await resolve_session(db, settings.auth, token) if token else None
    if session is None or str(session.user_id) != flow.user_id:
        return fail("session_mismatch")
    if error or not code:
        return fail("consent_denied")
    if not settings.graph.enabled or settings.graph.client_secret is None:
        return fail("not_configured")

    shared = flow.shared_mailbox is not None
    try:
        async with httpx.AsyncClient(timeout=settings.graph.timeout) as http:
            tokens = await exchange_code(
                http,
                settings.graph,
                code=code,
                redirect_uri=flow.redirect_uri,
                verifier=flow.verifier,
                scopes=delegated_scopes(settings.graph, shared=shared),
            )
        if not tokens.refresh_token:
            return fail("offline_access_missing")
        profile = await _profile(settings, tokens, flow.shared_mailbox)
    except AuthenticationError as exc:
        return fail("access_denied" if exc.code == "access_denied" else "token_exchange_failed")
    except ProviderError as exc:
        return fail(exc.code[:64])

    provider_settings = {"auth": "delegated", "user": flow.shared_mailbox or "me"}
    if profile.user_id:
        provider_settings["user_id"] = profile.user_id
    mailbox, created = await save_connected_mailbox(
        db,
        session.user_id,
        address=profile.address,
        display_name=profile.display_name,
        provider_settings=provider_settings,
        tokens=tokens,
    )
    log.info("graph_mailbox_connected", mailbox_id=str(mailbox.id), created=created, shared=shared)
    return done({"graph": "connected", "mailbox_id": str(mailbox.id)})


# -- change notifications -------------------------------------------------------------------

_queue_lock = asyncio.Lock()


async def _request_sync(mailbox_id: uuid.UUID) -> None:
    # The API process does not keep the job queue open; open it for this defer only.
    from app.mail.sync.tasks import request_sync
    from app.worker import app as worker_app

    async with _queue_lock, worker_app.open_async():
        await request_sync(mailbox_id)


def get_sync_requester() -> RequestSync:
    """Queues ``mail.sync_mailbox``; overridden in tests."""
    return _request_sync


@router.post(
    "/notifications",
    status_code=202,
    include_in_schema=False,
    response_model=None,
)
async def graph_notifications(
    request: Request,
    request_sync: Annotated[RequestSync, Depends(get_sync_requester)],
    validation_token: Annotated[str | None, Query(alias="validationToken")] = None,
) -> Response:
    settings = get_settings_from_app(request)
    if settings.graph.notification_url is None:
        raise ProblemError(404, detail="Not found.")
    if validation_token is not None:
        # Subscription validation: echo the token as plain text within 10 seconds.
        return PlainTextResponse(validation_token, headers={"X-Content-Type-Options": "nosniff"})
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_NOTIFICATION_BYTES:
            raise ProblemError(413, detail="Payload too large.")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    values = payload.get("value") if isinstance(payload, dict) else None
    items = [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    mailbox_ids: set[uuid.UUID] = set()
    rejected = 0
    for item in items:
        mailbox_id = verify_client_state(settings.security, item.get("clientState"))
        if mailbox_id is None:
            rejected += 1
        else:
            mailbox_ids.add(mailbox_id)
    for mailbox_id in mailbox_ids:
        try:
            await request_sync(mailbox_id)
        except Exception as exc:
            # Polling catches up; Graph must not retry the notification forever.
            log.warning(
                "graph_notification_sync_failed",
                mailbox_id=str(mailbox_id),
                error_type=type(exc).__name__,
            )
    log.info(
        "graph_notifications_received",
        notifications=len(items),
        mailboxes=len(mailbox_ids),
        rejected=rejected,
    )
    return Response(status_code=202)

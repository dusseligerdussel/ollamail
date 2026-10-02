"""Microsoft To Do: connect the account with OAuth, pick a list
(docs/providers/microsoft365.md §11).

To Do needs the delegated permission ``Tasks.ReadWrite``, which the mailbox connect flow
does not ask for, so the export has its own flow (incremental consent, same Entra app):

* ``POST /todo-export/mstodo/connect`` returns the Microsoft sign-in URL. ``state``, PKCE
  verifier and user travel in a short-lived cookie, encrypted and authenticated with a key
  derived from ``OLLAMAIL_SECRET_KEY``.
* ``GET /todo-export/mstodo/callback`` checks ``state`` and the signed-in user, exchanges
  the code and keeps the tokens in a second such cookie (10 minutes, ``HttpOnly``) until
  the user has chosen a list; then redirects to the settings with ``mstodo=connected`` or
  ``mstodo=error&reason=<code>``. Nothing is stored before the user saves.
* ``POST /todo-export/mstodo/lists``: lists of the signed-in (or already connected) account.
* ``PUT /todo-export/mstodo``: save list and mode; the tokens go encrypted into
  ``todo_export_targets.config``. Another account starts over (like another CalDAV server).

Changing the mode, syncing and disconnecting use the generic ``/todo-export`` endpoints.
Tokens, codes and Graph answers are never logged.
"""

import base64
import binascii
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Annotated, Any

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.auth.dependencies import CurrentSessionDep, SettingsDep, get_settings_from_app
from app.auth.keys import derive_key
from app.auth.redirect_flow import api_url, public_origin, safe_return_to
from app.auth.sessions import SESSION_COOKIE, resolve_session
from app.core.config import Settings
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.mail.providers.base import AuthenticationError, ProviderError
from app.mail.providers.graph_auth import TokenSet, authorization_url, code_verifier, exchange_code
from app.mail.providers.graph_client import GraphClient
from app.todos.export import service
from app.todos.export.base import SinkError, TaskList
from app.todos.export.models import TodoExportTarget
from app.todos.export.mstodo import todo_scopes
from app.todos.export.registry import available_sinks
from app.todos.export.router import (
    CONNECTION_ERRORS,
    DbDep,
    EnqueuerDep,
    SinkBuilderDep,
    _audit,
    _read,
    _sink_problem,
)
from app.todos.export.schemas import (
    ExportSettingsRead,
    MsTodoConnectRequest,
    MsTodoConnectResponse,
    MsTodoLists,
    MsTodoTargetSave,
    TaskListRead,
)

log = get_logger(__name__)

router = APIRouter(
    prefix="/todo-export/mstodo",
    tags=["todos"],
    responses={401: {"description": "Not signed in"}},
)

SINK = "mstodo"
FLOW_COOKIE = "ollamail_mstodo_flow"
TOKEN_COOKIE = "ollamail_mstodo_tokens"
COOKIE_LIFETIME_SECONDS = 600
CALLBACK_PATH = "/todo-export/mstodo/callback"
DEFAULT_RETURN = "/settings/task-export"
_NONCE_LEN = 12
_FLOW_AAD = b"ollamail mstodo flow v1"
_TOKEN_AAD = b"ollamail mstodo tokens v1"
# Browsers drop larger cookies; such a token set is rejected instead.
_MAX_COOKIE = 3800


def _key(settings: Settings) -> bytes:
    return derive_key(settings.security, "mstodo-connect")


def seal(settings: Settings, payload: dict[str, Any], aad: bytes) -> str:
    nonce = os.urandom(_NONCE_LEN)
    data = AESGCM(_key(settings)).encrypt(nonce, json.dumps(payload).encode(), aad)
    return base64.urlsafe_b64encode(nonce + data).rstrip(b"=").decode()


def unseal(
    settings: Settings, value: str | None, aad: bytes, *, now: float | None = None
) -> dict[str, Any] | None:
    """The payload, or ``None`` if missing, tampered with or expired."""
    if not value:
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        plain = AESGCM(_key(settings)).decrypt(raw[:_NONCE_LEN], raw[_NONCE_LEN:], aad)
        payload = json.loads(plain)
    except (binascii.Error, ValueError, InvalidTag, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("expires_at", 0)) < (time.time() if now is None else now):
        return None
    return payload


def _set_cookie(response: Response, settings: Settings, name: str, value: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=COOKIE_LIFETIME_SECONDS,
        path="/",
        secure=settings.auth.cookie_secure,
        httponly=True,
        # Sent on the top-level redirect back from Microsoft.
        samesite="lax",
    )


def _require_sink(settings: Settings) -> None:
    if SINK not in available_sinks(settings.todos):
        raise ProblemError(
            422, detail="This export target is not enabled.", error_code="sink_not_available"
        )
    if not settings.graph.enabled or settings.graph.client_secret is None:
        raise ProblemError(
            422, detail="Microsoft 365 is not configured.", error_code="not_configured"
        )


@router.post("/connect", responses={422: {"description": "Target not enabled or configured"}})
async def connect_mstodo(
    body: MsTodoConnectRequest,
    request: Request,
    response: Response,
    current: CurrentSessionDep,
    settings: SettingsDep,
) -> MsTodoConnectResponse:
    """Start the Microsoft sign-in for the To Do export; the client navigates to the URL."""
    _require_sink(settings)
    state, verifier = secrets.token_urlsafe(32), code_verifier()
    redirect_uri = api_url(settings, request, CALLBACK_PATH)
    flow = {
        "state": state,
        "verifier": verifier,
        "user_id": str(current.user_id),
        "redirect_uri": redirect_uri,
        "return_to": safe_return_to(body.return_to or DEFAULT_RETURN),
        "expires_at": int(time.time()) + COOKIE_LIFETIME_SECONDS,
    }
    url = authorization_url(
        settings.graph,
        redirect_uri=redirect_uri,
        state=state,
        verifier=verifier,
        scopes=todo_scopes(settings.graph),
    )
    _set_cookie(response, settings, FLOW_COOKIE, seal(settings, flow, _FLOW_AAD))
    return MsTodoConnectResponse(authorization_url=url)


async def _account(settings: Settings, tokens: TokenSet) -> tuple[str, str]:
    """Object ID and sign-in name of the account that consented."""

    async def token(force: bool) -> str:
        return tokens.access_token

    client = GraphClient(
        api_url=settings.graph.api_url,
        token=token,
        timeout=settings.graph.timeout,
        max_retries=settings.graph.max_retries,
    )
    try:
        me = await client.get_json("/me", params={"$select": "id,mail,userPrincipalName"})
    finally:
        await client.aclose()
    account_id = me.get("id")
    name = me.get("userPrincipalName") or me.get("mail")
    if not isinstance(account_id, str) or not isinstance(name, str) or not name:
        raise ProviderError(code="invalid_response")
    return account_id, name


@router.get("/callback", name="mstodo_callback", include_in_schema=False)
async def mstodo_callback(
    request: Request,
    db: DbDep,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    settings = get_settings_from_app(request)
    flow = unseal(settings, request.cookies.get(FLOW_COOKIE), _FLOW_AAD)
    return_to = safe_return_to(flow.get("return_to")) if flow else DEFAULT_RETURN

    def done(params: str, tokens: str | None = None) -> RedirectResponse:
        separator = "&" if "?" in return_to else "?"
        response = RedirectResponse(f"{return_to}{separator}{params}", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/")
        if tokens is not None:
            _set_cookie(response, settings, TOKEN_COOKIE, tokens)
        return response

    def fail(reason: str) -> RedirectResponse:
        log.info("mstodo_connect_failed", reason=reason)
        return done(f"mstodo=error&reason={reason}")

    if flow is None or not state or not hmac.compare_digest(state, str(flow.get("state"))):
        return fail("state_invalid")
    token = request.cookies.get(SESSION_COOKIE)
    session = await resolve_session(db, settings.auth, token) if token else None
    if session is None or str(session.user_id) != flow.get("user_id"):
        return fail("session_mismatch")
    if error or not code:
        return fail("consent_denied")
    if not settings.graph.enabled or settings.graph.client_secret is None:
        return fail("not_configured")
    try:
        async with httpx.AsyncClient(timeout=settings.graph.timeout) as http:
            tokens = await exchange_code(
                http,
                settings.graph,
                code=code,
                redirect_uri=str(flow.get("redirect_uri")),
                verifier=str(flow.get("verifier")),
                scopes=todo_scopes(settings.graph),
            )
        if not tokens.refresh_token:
            return fail("offline_access_missing")
        account_id, account = await _account(settings, tokens)
    except AuthenticationError as exc:
        return fail("access_denied" if exc.code == "access_denied" else "token_exchange_failed")
    except ProviderError as exc:
        return fail(exc.code[:64])
    # The access token is left out to keep the cookie small; the sink refreshes it.
    sealed = seal(
        settings,
        {
            "user_id": str(session.user_id),
            "account_id": account_id,
            "username": account,
            "refresh_token": tokens.refresh_token,
            "expires_at": int(time.time()) + COOKIE_LIFETIME_SECONDS,
        },
        _TOKEN_AAD,
    )
    if len(sealed) > _MAX_COOKIE:
        return fail("token_too_large")
    log.info("mstodo_signed_in", user_id=str(session.user_id))
    return done("mstodo=connected", sealed)


def _signed_in(request: Request, settings: Settings, user_id: uuid.UUID) -> dict[str, Any] | None:
    """Config from a sign-in not saved yet (token cookie of this user)."""
    payload = unseal(settings, request.cookies.get(TOKEN_COOKIE), _TOKEN_AAD)
    if payload is None or payload.get("user_id") != str(user_id):
        return None
    return {
        "account_id": str(payload.get("account_id", "")),
        "username": str(payload.get("username", "")),
        "refresh_token": str(payload.get("refresh_token", "")),
    }


def _config(
    request: Request, settings: Settings, user_id: uuid.UUID, target: TodoExportTarget | None
) -> tuple[dict[str, Any], bool]:
    """Config to use and whether it comes from a new sign-in."""
    config = _signed_in(request, settings, user_id)
    if config is not None:
        return config, True
    if target is not None and target.sink == SINK:
        return dict(target.config), False
    raise ProblemError(
        409, detail="Sign in with Microsoft first.", error_code="mstodo_not_connected"
    )


async def _lists(
    sink_builder: service.SinkBuilder, config: dict[str, Any], settings: Settings
) -> tuple[list[TaskList], dict[str, Any]]:
    """Lists of the account, and the config with tokens the sink may have rotated."""
    try:
        sink = sink_builder(SINK, config, settings.todos)
    except SinkError as exc:
        raise _sink_problem(exc) from None
    try:
        lists = await sink.list_task_lists()
    except SinkError as exc:
        raise _sink_problem(exc) from None
    finally:
        await sink.aclose()
    updated = sink.updated_config()
    return lists, ({**config, **updated} if updated is not None else config)


def _keep_tokens(
    request: Request, response: Response, settings: Settings, config: dict[str, Any]
) -> None:
    """Re-seal a sign-in that is not saved yet with the latest refresh token."""
    payload = unseal(settings, request.cookies.get(TOKEN_COOKIE), _TOKEN_AAD)
    if payload is not None and payload.get("refresh_token") != config.get("refresh_token"):
        payload["refresh_token"] = config.get("refresh_token")
        _set_cookie(response, settings, TOKEN_COOKIE, seal(settings, payload, _TOKEN_AAD))


CONNECT_ERRORS: dict[int | str, dict[str, Any]] = {
    **CONNECTION_ERRORS,
    409: {"description": "No Microsoft sign-in and no Microsoft To Do export"},
}


@router.post("/lists", responses=CONNECT_ERRORS)
async def list_mstodo_lists(
    request: Request,
    response: Response,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    sink_builder: SinkBuilderDep,
) -> MsTodoLists:
    """Lists of the Microsoft account just signed in with, or of the connected one."""
    _require_sink(settings)
    target = await service.get_target(db, current.user_id)
    config, new = _config(request, settings, current.user_id, target)
    lists, config = await _lists(sink_builder, config, settings)
    if new:
        _keep_tokens(request, response, settings, config)
    elif target is not None and target.config.get("refresh_token") != config.get("refresh_token"):
        target.config = config
        await db.commit()
    return MsTodoLists(
        account=str(config.get("username", "")),
        lists=[TaskListRead(id=item.id, name=item.name) for item in lists],
    )


@router.put("", responses=CONNECT_ERRORS)
async def save_mstodo(
    body: MsTodoTargetSave,
    request: Request,
    response: Response,
    current: CurrentSessionDep,
    db: DbDep,
    settings: SettingsDep,
    sink_builder: SinkBuilderDep,
    enqueue: EnqueuerDep,
) -> ExportSettingsRead:
    """Connect the Microsoft To Do export (or change list or mode). The list must be one of
    the account's; open todos are exported right away in mode ``auto``."""
    _require_sink(settings)
    target = await service.get_target(db, current.user_id)
    config, new = _config(request, settings, current.user_id, target)
    lists, config = await _lists(sink_builder, config, settings)
    chosen = next((item for item in lists if item.id == body.list_id), None)
    if chosen is None:
        raise ProblemError(422, detail="Unknown list.", error_code="unknown_list")
    if target is not None and (
        target.sink != SINK or target.config.get("account_id") != config.get("account_id")
    ):
        # Another target or account: the old references mean nothing there.
        await service.forget_refs(db, target)
        await db.delete(target)
        await db.flush()
        target = None
    change = "updated" if target is not None else "connected"
    if target is None:
        target = TodoExportTarget(user_id=current.user_id, sink=SINK)
        db.add(target)
    elif new:
        # Same account signed in again: new tokens, keep the status sync state.
        config = {**target.config, **config}
    target.config = config
    target.list_id = chosen.id
    target.list_name = chosen.name
    target.mode = body.mode
    target.app_url = public_origin(settings, request)
    target.last_error = None
    target.next_poll_at = None
    await db.flush()
    await _audit(db, current.user_id, target, change)
    await db.commit()
    response.delete_cookie(TOKEN_COOKIE, path="/")
    await enqueue(target.id)
    return await _read(db, settings, target)

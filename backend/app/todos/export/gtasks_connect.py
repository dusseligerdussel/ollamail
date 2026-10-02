"""Connecting Google Tasks as export target with OAuth (#102, docs/providers/gmail.md §8).

Same building blocks as the Gmail connect flow (``app/mail/providers/gmail_connect.py``):

* ``POST /todo-export/gtasks/oauth/start`` returns Google's authorization URL for the scope
  ``tasks`` (``include_granted_scopes``). ``state``, PKCE verifier, user and the chosen
  export mode go into a short-lived, HMAC-signed ``HttpOnly`` cookie.
* ``GET /todo-export/gtasks/oauth/callback`` checks cookie, ``state`` and user, exchanges
  the code, checks that ``tasks`` was granted, lists the task lists and saves the target:
  a new one exports to the default list (the first one), the user can pick another list
  afterwards (``PATCH /todo-export``). Reconnecting the same account (its list still
  exists) only replaces the refresh token; another account starts over. The browser is
  redirected to ``/settings/task-export?gtasks=connected`` or ``?gtasks_error=<code>``.

Only the refresh token is stored, encrypted in ``todo_export_targets.config``. Neither
tokens nor list names are logged; connecting is in the audit log (``todo_export.changed``).
"""

import hmac
import secrets
import time
from typing import Annotated
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.auth.redirect_flow import public_origin
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.mail.providers.base import ProviderError
from app.mail.providers.gmail_auth import (
    SCOPE_TASKS,
    authorization_url,
    exchange_code,
    oauth_configured,
    pkce_pair,
)
from app.mail.providers.gmail_connect import decode_state_cookie, encode_state_cookie
from app.todos.export import service
from app.todos.export.base import SinkError
from app.todos.export.gtasks import GoogleTasksSink
from app.todos.export.models import ExportMode, TodoExportTarget
from app.todos.export.registry import available_sinks
from app.todos.export.router import EnqueuerDep, SinkBuilderDep, _audit

log = get_logger(__name__)

SINK = GoogleTasksSink.kind
STATE_COOKIE = "ollamail_gtasks_oauth"
STATE_TTL_SECONDS = 600
# Separates these cookies from the Gmail flow's, which use the same signing key.
PURPOSE = "todo_export_gtasks"
CALLBACK_PATH = "/todo-export/gtasks/oauth/callback"
SETTINGS_PAGE = "/settings/task-export"

router = APIRouter(
    prefix="/todo-export/gtasks",
    tags=["todos"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]


class GoogleTasksOAuthStart(BaseModel):
    # Export mode of a new connection; a reconnect keeps the current one.
    mode: ExportMode = ExportMode.AUTO


class GoogleTasksOAuthStartResponse(BaseModel):
    authorization_url: str


def redirect_uri(settings: Settings) -> str | None:
    """``OLLAMAIL_TODOS_EXPORT_GTASKS_REDIRECT_URI``, else derived from the Gmail one."""
    if settings.todos.export_gtasks_redirect_uri:
        return settings.todos.export_gtasks_redirect_uri
    gmail = settings.gmail.redirect_uri
    if not gmail:
        return None
    suffix = "/mail/gmail/oauth/callback"
    if gmail.rstrip("/").endswith(suffix):
        return gmail.rstrip("/")[: -len(suffix)] + CALLBACK_PATH
    parts = urlsplit(gmail)
    return f"{parts.scheme}://{parts.netloc}/api{CALLBACK_PATH}"


def _configured(settings: Settings) -> bool:
    return oauth_configured(settings.gmail) and redirect_uri(settings) is not None


@router.post(
    "/oauth/start",
    responses={
        422: {"description": "Google Tasks is not enabled as export target"},
        503: {"description": "Google OAuth is not configured"},
    },
)
async def start_gtasks_oauth(
    body: GoogleTasksOAuthStart,
    current: CurrentSessionDep,
    settings: SettingsDep,
    response: Response,
) -> GoogleTasksOAuthStartResponse:
    """Start connecting Google Tasks: open the returned URL in the browser."""
    if SINK not in available_sinks(settings.todos):
        raise ProblemError(
            422, detail="This export target is not enabled.", error_code="sink_not_available"
        )
    if not _configured(settings):
        raise ProblemError(
            503, detail="Google OAuth is not configured.", error_code="oauth_not_configured"
        )
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    cookie = encode_state_cookie(
        settings,
        {
            "purpose": PURPOSE,
            "state": state,
            "verifier": verifier,
            "user": str(current.user_id),
            "mode": body.mode.value,
            "exp": time.time() + STATE_TTL_SECONDS,
        },
    )
    response.set_cookie(
        STATE_COOKIE,
        cookie,
        max_age=STATE_TTL_SECONDS,
        path="/",
        secure=settings.auth.cookie_secure,
        httponly=True,
        # Lax: the cookie is sent on the top-level redirect back from Google.
        samesite="lax",
    )
    url = authorization_url(
        settings.gmail,
        state=state,
        code_challenge=challenge,
        scope=SCOPE_TASKS,
        redirect_uri=redirect_uri(settings),
    )
    return GoogleTasksOAuthStartResponse(authorization_url=url)


def _redirect(settings: Settings, **params: str) -> RedirectResponse:
    response = RedirectResponse(f"{SETTINGS_PAGE}?{urlencode(params)}", status_code=303)
    response.delete_cookie(
        STATE_COOKIE, path="/", secure=settings.auth.cookie_secure, httponly=True, samesite="lax"
    )
    return response


@router.get(
    "/oauth/callback",
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "Back to the settings with the result in the query"}},
)
async def gtasks_oauth_callback(
    request: Request,
    current: CurrentSessionDep,
    settings: SettingsDep,
    db: DbDep,
    sink_builder: SinkBuilderDep,
    enqueue: EnqueuerDep,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    """Redirect target registered at Google; finishes connecting Google Tasks."""
    payload = decode_state_cookie(settings, request.cookies.get(STATE_COOKIE))
    if (
        payload is None
        or payload.get("purpose") != PURPOSE
        or state is None
        or not hmac.compare_digest(str(payload.get("state", "")), state)
        or payload.get("user") != str(current.user_id)
    ):
        return _redirect(settings, gtasks_error="invalid_state")
    if error is not None or code is None:
        # The user cancelled at Google (``access_denied``) or Google refused the request.
        return _redirect(settings, gtasks_error="consent_denied")
    if SINK not in available_sinks(settings.todos):
        return _redirect(settings, gtasks_error="sink_not_available")
    try:
        async with httpx.AsyncClient(timeout=settings.todos.export_timeout_seconds) as http:
            grant = await exchange_code(
                http,
                settings.gmail,
                code,
                str(payload["verifier"]),
                redirect_uri=redirect_uri(settings),
            )
    except ProviderError as exc:
        log.warning("gtasks_connect_failed", user_id=str(current.user_id), error=exc.code)
        return _redirect(settings, gtasks_error=exc.code)
    if SCOPE_TASKS not in grant.scopes:
        return _redirect(settings, gtasks_error="insufficient_scope")
    if grant.refresh_token is None:
        return _redirect(settings, gtasks_error="refresh_token_missing")

    config = {"refresh_token": grant.refresh_token}
    sink = sink_builder(SINK, config, settings.todos)
    try:
        lists = await sink.list_task_lists()
    except SinkError as exc:
        log.warning("gtasks_connect_failed", user_id=str(current.user_id), error=exc.code)
        return _redirect(settings, gtasks_error=exc.code)
    finally:
        await sink.aclose()
    if not lists:
        return _redirect(settings, gtasks_error="no_lists")

    target = await service.get_target(db, current.user_id)
    same_account = (
        target is not None
        and target.sink == SINK
        and any(item.id == target.list_id for item in lists)
    )
    if target is not None and not same_account:
        # Another target system or Google account: the old references mean nothing here.
        await service.forget_refs(db, target)
        await db.delete(target)
        await db.flush()
        target = None
    if target is None:
        chosen = lists[0]
        target = TodoExportTarget(
            user_id=current.user_id,
            sink=SINK,
            list_id=chosen.id,
            list_name=chosen.name,
            mode=ExportMode(payload.get("mode", ExportMode.AUTO.value)),
        )
        db.add(target)
        change = "connected"
    else:
        change = "updated"
    target.config = config
    target.app_url = public_origin(settings, request)
    target.last_error = None
    target.next_poll_at = None
    await db.flush()
    await _audit(db, current.user_id, target, change)
    await db.commit()
    await enqueue(target.id)
    log.info("gtasks_connected", user_id=str(current.user_id), target_id=str(target.id))
    return _redirect(settings, gtasks="connected")

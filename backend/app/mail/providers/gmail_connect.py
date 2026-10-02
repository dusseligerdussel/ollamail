"""Connecting a Gmail mailbox with OAuth (docs/providers/gmail.md §2.1).

Minimal building blocks until the mailbox API (#15) exists:

* ``POST /mail/gmail/oauth/start`` returns Google's authorization URL. ``state`` and the
  PKCE verifier go into a short-lived, HMAC-signed ``HttpOnly`` cookie (never into the URL).
* ``GET /mail/gmail/oauth/callback`` checks cookie, ``state`` and user, exchanges the code,
  checks the granted scope, reads the address from ``users/me/profile`` and creates the
  mailbox of the signed-in user (or stores the new refresh token of an existing one). The
  browser is redirected to ``/?mailbox_connected=<id>`` or ``/?mailbox_error=<code>``.

Only the refresh token is stored, encrypted (``Mailbox.credentials``). Neither tokens nor
addresses are logged. The watcher picks new mailboxes up within a minute and syncs them.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.auth.dependencies import CurrentSessionDep, SettingsDep
from app.auth.keys import derive_key
from app.core.config import Settings
from app.core.db import get_db
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.mail.models import Mailbox, MailboxType
from app.mail.providers.base import ProviderError
from app.mail.providers.gmail_api import GOOGLE_API
from app.mail.providers.gmail_auth import (
    authorization_url,
    exchange_code,
    oauth_configured,
    pkce_pair,
    scope_granted,
)

log = get_logger(__name__)

STATE_COOKIE = "ollamail_gmail_oauth"
STATE_TTL_SECONDS = 600

router = APIRouter(
    prefix="/mail/gmail",
    tags=["mail"],
    responses={401: {"description": "Not signed in"}},
)

DbDep = Annotated[AsyncSession, Depends(get_db)]


class GmailOAuthStart(BaseModel):
    # Pre-fills the Google account chooser.
    login_hint: str | None = Field(default=None, max_length=320)


class GmailOAuthStartResponse(BaseModel):
    authorization_url: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _state_key(settings: Settings) -> bytes:
    return derive_key(settings.security, "gmail oauth state")


def encode_state_cookie(settings: Settings, payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_state_key(settings), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def decode_state_cookie(settings: Settings, value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value:
        return None
    body, _, mac = value.partition(".")
    expected = _b64(hmac.new(_state_key(settings), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(payload, dict) or float(payload.get("exp", 0)) < time.time():
        return None
    return payload


@router.post("/oauth/start", responses={503: {"description": "Gmail OAuth is not configured"}})
async def start_gmail_oauth(
    body: GmailOAuthStart,
    current: CurrentSessionDep,
    settings: SettingsDep,
    response: Response,
) -> GmailOAuthStartResponse:
    """Start connecting a Gmail mailbox: open the returned URL in the browser."""
    if not oauth_configured(settings.gmail):
        raise ProblemError(503, detail="Gmail OAuth is not configured.")
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    cookie = encode_state_cookie(
        settings,
        {
            "state": state,
            "verifier": verifier,
            "user": str(current.user_id),
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
        settings.gmail, state=state, code_challenge=challenge, login_hint=body.login_hint
    )
    return GmailOAuthStartResponse(authorization_url=url)


def _redirect(settings: Settings, **params: str) -> RedirectResponse:
    response = RedirectResponse(f"/?{urlencode(params)}", status_code=303)
    response.delete_cookie(
        STATE_COOKIE, path="/", secure=settings.auth.cookie_secure, httponly=True, samesite="lax"
    )
    return response


async def _profile_address(http: httpx.AsyncClient, access_token: str) -> str:
    try:
        response = await http.get(
            f"{GOOGLE_API}/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError:
        raise ProviderError(code="connection_failed") from None
    if response.status_code != 200:
        raise ProviderError(code="profile_failed")
    try:
        address = response.json()["emailAddress"]
    except (ValueError, KeyError, TypeError):
        raise ProviderError(code="profile_failed") from None
    if not isinstance(address, str) or "@" not in address:
        raise ProviderError(code="profile_failed")
    return address


async def connect_mailbox(
    db: AsyncSession, user_id: uuid.UUID, address: str, refresh_token: str | None
) -> Mailbox:
    """Create the user's Gmail mailbox for ``address`` (audited as ``mailbox.created``) or
    update its refresh token."""
    mailbox = await db.scalar(
        select(Mailbox).where(
            Mailbox.owner_user_id == user_id,
            Mailbox.type == MailboxType.GMAIL,
            func.lower(Mailbox.address) == address.lower(),
        )
    )
    if mailbox is None:
        if refresh_token is None:
            raise ProviderError(code="refresh_token_missing")
        mailbox = Mailbox(
            type=MailboxType.GMAIL,
            display_name=address,
            address=address,
            owner_user_id=user_id,
            provider_settings={"auth": "oauth"},
            credentials={"refresh_token": refresh_token},
        )
        db.add(mailbox)
        await db.flush()
        await audit.record(
            db,
            audit.Actor.user(user_id),
            audit.AuditAction.MAILBOX_CREATED,
            audit.Target.of(audit.TargetType.MAILBOX, mailbox.id),
            {"type": MailboxType.GMAIL.value},
        )
    elif refresh_token is not None:
        mailbox.credentials = {**(mailbox.credentials or {}), "refresh_token": refresh_token}
    await db.commit()
    return mailbox


@router.get(
    "/oauth/callback",
    status_code=303,
    response_class=RedirectResponse,
    responses={303: {"description": "Back to the app with the result in the query"}},
)
async def gmail_oauth_callback(
    request: Request,
    current: CurrentSessionDep,
    settings: SettingsDep,
    db: DbDep,
    code: Annotated[str | None, Query(max_length=2048)] = None,
    state: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    """Redirect target registered at Google; finishes connecting the mailbox."""
    payload = decode_state_cookie(settings, request.cookies.get(STATE_COOKIE))
    if (
        payload is None
        or state is None
        or not hmac.compare_digest(str(payload.get("state", "")), state)
        or payload.get("user") != str(current.user_id)
    ):
        return _redirect(settings, mailbox_error="invalid_state")
    if error is not None or code is None:
        # The user cancelled at Google (``access_denied``) or Google refused the request.
        return _redirect(settings, mailbox_error="access_denied")
    try:
        async with httpx.AsyncClient(timeout=settings.gmail.timeout) as http:
            grant = await exchange_code(http, settings.gmail, code, str(payload["verifier"]))
            if not scope_granted(settings.gmail, grant.scopes):
                return _redirect(settings, mailbox_error="insufficient_scope")
            address = await _profile_address(http, grant.access_token)
        mailbox = await connect_mailbox(db, current.user_id, address, grant.refresh_token)
    except ProviderError as exc:
        log.warning("gmail_connect_failed", user_id=str(current.user_id), error=exc.code)
        return _redirect(settings, mailbox_error=exc.code)
    log.info("gmail_connected", user_id=str(current.user_id), mailbox_id=str(mailbox.id))
    return _redirect(settings, mailbox_connected=str(mailbox.id))

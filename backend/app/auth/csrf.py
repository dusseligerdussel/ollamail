"""CSRF protection: signed double-submit cookie.

Every state-changing request (``POST``, ``PUT``, ``PATCH``, ``DELETE``, ...) must send the
value of the ``ollamail_csrf`` cookie in the ``X-CSRF-Token`` header. The token is
``<nonce>.<HMAC(nonce, session cookie)>``: it is bound to the current session, so a cookie
planted from a sibling subdomain does not validate, and it changes on every login and
logout. Requests the browser marks as cross-site (``Sec-Fetch-Site``) are rejected as
well. The session cookie additionally uses ``SameSite=Lax``.

The middleware covers the whole app (closed by default). ``exempt_paths`` lists the few
endpoints that are called by other servers without cookies and authenticate requests
themselves (e.g. Microsoft Graph change notifications); entries ending in ``/`` exempt
every path below them (SCIM, #95). Responses get a fresh cookie when
the request had none or one bound to a different session.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Iterable

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth.keys import derive_key
from app.auth.sessions import SESSION_COOKIE
from app.core.config import Settings
from app.core.errors import problem_response

CSRF_COOKIE = "ollamail_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_NONCE_BYTES = 16
_MAC_BYTES = 16


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _mac(key: bytes, nonce: str, session_token: str) -> str:
    message = f"{nonce}.".encode() + hashlib.sha256(session_token.encode()).digest()
    return _b64(hmac.new(key, message, hashlib.sha256).digest()[:_MAC_BYTES])


def _key(settings: Settings) -> bytes:
    return derive_key(settings.security, "csrf")


def issue_csrf_token(settings: Settings, session_token: str | None) -> str:
    nonce = _b64(secrets.token_bytes(_NONCE_BYTES))
    return f"{nonce}.{_mac(_key(settings), nonce, session_token or '')}"


def csrf_token_valid(settings: Settings, token: str | None, session_token: str | None) -> bool:
    if not token or token.count(".") != 1:
        return False
    nonce, mac = token.split(".")
    return hmac.compare_digest(mac, _mac(_key(settings), nonce, session_token or ""))


def set_csrf_cookie(response: Response, settings: Settings, session_token: str | None) -> None:
    """Issue a token for ``session_token`` (call after login and logout)."""
    response.set_cookie(
        CSRF_COOKIE,
        issue_csrf_token(settings, session_token),
        max_age=settings.auth.session_lifetime_minutes * 60,
        path="/",
        secure=settings.auth.cookie_secure,
        # Readable by the frontend, which echoes it in the header.
        httponly=False,
        samesite="strict",
    )


class CSRFMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, exempt_paths: Iterable[str] = ()) -> None:
        self.app = app
        self.settings = settings
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        session_token = request.cookies.get(SESSION_COOKIE)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        cookie_valid = csrf_token_valid(self.settings, cookie_token, session_token)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and not cookie_valid:
                headers = MutableHeaders(scope=message)
                already_set = any(
                    value.startswith(f"{CSRF_COOKIE}=") for value in headers.getlist("set-cookie")
                )
                if not already_set:
                    carrier = Response()
                    set_csrf_cookie(carrier, self.settings, session_token)
                    headers.append("set-cookie", carrier.headers["set-cookie"])
            await send(message)

        if request.method not in SAFE_METHODS and not self._exempt(scope):
            header_token = request.headers.get(CSRF_HEADER)
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            matches = (
                cookie_valid
                and header_token is not None
                and cookie_token is not None
                and hmac.compare_digest(header_token, cookie_token)
            )
            if cross_site or not matches:
                response = problem_response(403, detail="CSRF token missing or invalid.")
                await response(scope, receive, send_with_cookie)
                return

        await self.app(scope, receive, send_with_cookie)

    def _exempt(self, scope: Scope) -> bool:
        if not self.exempt_paths:
            return False
        path: str = scope["path"]
        root_path: str = scope.get("root_path", "")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :]
        # Entries ending in "/" exempt everything below them (SCIM, bearer tokens).
        return path in self.exempt_paths or any(
            path.startswith(exempt) for exempt in self.exempt_paths if exempt.endswith("/")
        )

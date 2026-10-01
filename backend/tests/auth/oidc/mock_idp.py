"""In-process mock OpenID Provider for tests (ASGI app behind ``httpx.ASGITransport``).

Implements discovery, JWKS, authorize (auto-consent), token (client authentication,
redirect URI and PKCE S256 checks), userinfo and end_session. Tests tamper with the ID
token through ``claims`` overrides, ``signing_key`` or ``alg``.
"""

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode

from joserfc import jwt
from joserfc.jwk import KeySet, OctKey, RSAKey
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

ISSUER = "https://idp.test/realms/test"
CLIENT_ID = "ollamail"
CLIENT_SECRET = "s3cret-client-for-tests"


@dataclass
class Grant:
    client_id: str
    redirect_uri: str
    code_challenge: str
    nonce: str
    scope: str


@dataclass
class MockIdP:
    issuer: str = ISSUER
    # URL the endpoints live under (default: the issuer).
    base: str | None = None
    # "issuer" in the discovery document (default: the issuer).
    discovery_issuer: str | None = None
    client_id: str = CLIENT_ID
    client_secret: str | None = CLIENT_SECRET
    key: RSAKey = field(default_factory=lambda: RSAKey.generate_key(2048, parameters={"kid": "k1"}))
    # Signs ID tokens instead of ``key`` (e.g. an attacker's key).
    signing_key: Any = None
    alg: str = "RS256"
    # Claims of the next ID tokens; ``None`` values remove a claim.
    subject: str = "user-123"
    claims: dict[str, Any] = field(default_factory=dict)
    userinfo: dict[str, Any] | None = None
    token_endpoint_auth_methods: list[str] = field(
        default_factory=lambda: ["client_secret_basic", "client_secret_post"]
    )
    grants: dict[str, Grant] = field(default_factory=dict)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    discovery_requests: int = 0
    jwks_requests: int = 0

    def default_claims(self, grant: Grant) -> dict[str, Any]:
        now = int(time.time())
        return {
            "iss": self.issuer,
            "sub": self.subject,
            "aud": grant.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant.nonce,
            "email": "erika@example.org",
            "email_verified": True,
            "name": "Erika Mustermann",
            "groups": ["staff", "mail-admins"],
        }

    def id_token(self, grant: Grant) -> str:
        claims = self.default_claims(grant)
        for key, value in self.claims.items():
            if value is None:
                claims.pop(key, None)
            else:
                claims[key] = value
        key = self.signing_key or self.key
        header = {"alg": self.alg, "kid": key.kid} if key.kid else {"alg": self.alg}
        if self.alg == "none":
            raw = base64.urlsafe_b64encode
            body = raw(json.dumps(header).encode()).rstrip(b"=").decode()
            payload = raw(json.dumps(claims).encode()).rstrip(b"=").decode()
            return f"{body}.{payload}."
        return jwt.encode(header, claims, key, algorithms=[self.alg])

    # -- endpoints ---------------------------------------------------------------------

    async def discovery(self, request: Request) -> Response:
        self.discovery_requests += 1
        base = self.base or self.issuer
        return JSONResponse(
            {
                "issuer": self.discovery_issuer or self.issuer,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "jwks_uri": f"{base}/jwks",
                "userinfo_endpoint": f"{base}/userinfo",
                "end_session_endpoint": f"{base}/logout",
                "id_token_signing_alg_values_supported": ["RS256", "HS256", "none"],
                "token_endpoint_auth_methods_supported": self.token_endpoint_auth_methods,
                "code_challenge_methods_supported": ["S256"],
            }
        )

    async def jwks(self, request: Request) -> Response:
        self.jwks_requests += 1
        return JSONResponse(KeySet([self.key]).as_dict(private=False))

    async def authorize(self, request: Request) -> Response:
        q = request.query_params
        assert q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.grants[code] = Grant(
            client_id=q["client_id"],
            redirect_uri=q["redirect_uri"],
            code_challenge=q["code_challenge"],
            nonce=q["nonce"],
            scope=q["scope"],
        )
        return RedirectResponse(
            f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", 302
        )

    def _client_id(self, request: Request, form: dict[str, str]) -> str | None:
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            client_id, _, secret = base64.b64decode(header[6:]).decode().partition(":")
        else:
            client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
        if client_id != self.client_id:
            return None
        if self.client_secret is not None and secret != self.client_secret:
            return None
        return client_id

    async def token(self, request: Request) -> Response:
        form = dict(parse_qsl((await request.body()).decode()))
        self.token_requests.append(form)
        if self._client_id(request, form) is None:
            return JSONResponse({"error": "invalid_client"}, 401)
        grant = self.grants.pop(form.get("code", ""), None)
        if grant is None or form.get("redirect_uri") != grant.redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, 400)
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if challenge != grant.code_challenge:
            return JSONResponse({"error": "invalid_grant"}, 400)
        return JSONResponse(
            {
                "access_token": "access-" + secrets.token_urlsafe(8),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": self.id_token(grant),
            }
        )

    async def userinfo_endpoint(self, request: Request) -> Response:
        if not request.headers.get("authorization", "").startswith("Bearer access-"):
            return JSONResponse({"error": "invalid_token"}, 401)
        return JSONResponse(self.userinfo or {"sub": self.subject})

    def app(self) -> Starlette:
        base = self.base or self.issuer
        prefix = "/" + base.split("/", 3)[3] if base.count("/") > 2 else ""
        return Starlette(
            routes=[
                Route(f"{prefix}/.well-known/openid-configuration", self.discovery),
                Route(f"{prefix}/jwks", self.jwks),
                Route(f"{prefix}/authorize", self.authorize),
                Route(f"{prefix}/token", self.token, methods=["POST"]),
                Route(f"{prefix}/userinfo", self.userinfo_endpoint),
            ]
        )


def hmac_key(secret: str) -> OctKey:
    return OctKey.import_key(secret.encode())

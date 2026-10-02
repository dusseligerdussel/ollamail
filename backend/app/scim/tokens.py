"""Bearer tokens for SCIM clients and the authentication of SCIM requests.

A token is ``olm_scim_`` plus 256 random bits, shown once when an admin creates it; the
database keeps only its SHA-256 and a short hint. Revoking deletes the row, so the next
request with it fails. Every request is counted per token (``OLLAMAIL_SCIM_RATE_LIMIT_PER_MINUTE``);
requests with a missing or wrong token per client IP (``OLLAMAIL_SCIM_FAILED_AUTH_PER_IP``
within 15 minutes). Both use the fixed-window counters of the login (``app.auth.rate_limit``).
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import rate_limit
from app.auth.keys import derive_key, keyed_digest
from app.core.config import Settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.scim.errors import ScimError
from app.scim.models import ScimConfig, ScimToken

log = get_logger(__name__)

TOKEN_PREFIX = "olm_scim_"
_HINT_LENGTH = len(TOKEN_PREFIX) + 4
_RATE_WINDOW = timedelta(minutes=1)
_FAILED_WINDOW = timedelta(minutes=15)
# ``last_used_at`` is written at most this often per token.
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class IssuedToken:
    token: ScimToken
    secret: str


@dataclass(frozen=True)
class ScimClient:
    """The authenticated SCIM client of a request."""

    token_id: uuid.UUID


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_token(name: str, expires_at: datetime | None = None) -> IssuedToken:
    secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ScimToken(
        name=name,
        token_hash=hash_token(secret),
        hint=secret[:_HINT_LENGTH],
        expires_at=expires_at,
    )
    return IssuedToken(token=token, secret=secret)


async def get_config(db: AsyncSession) -> ScimConfig:
    """The stored settings, or an unsaved row with the defaults (SCIM off)."""
    config = await db.scalar(select(ScimConfig))
    if config is None:
        config = ScimConfig(enabled=False, link_providers=[])
    return config


async def get_config_for_update(db: AsyncSession) -> ScimConfig:
    config = await db.scalar(select(ScimConfig).with_for_update())
    if config is None:
        config = ScimConfig(enabled=False, link_providers=[])
        db.add(config)
        await db.flush()
    return config


def _bearer(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


def _unauthorized(detail: str = "A valid bearer token is required.") -> ScimError:
    return ScimError(401, detail, headers={"WWW-Authenticate": 'Bearer realm="scim"'})


def _too_many(hit: rate_limit.Hit) -> ScimError:
    return ScimError(429, "Too many requests.", headers={"Retry-After": str(hit.retry_after())})


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def _find(db: AsyncSession, secret: str) -> ScimToken | None:
    if not secret.startswith(TOKEN_PREFIX):
        return None
    token = await db.scalar(select(ScimToken).where(ScimToken.token_hash == hash_token(secret)))
    # The unique index already matched the hash; compare again in constant time anyway.
    if token is None or not hmac.compare_digest(token.token_hash, hash_token(secret)):
        return None
    if token.expires_at is not None and token.expires_at <= datetime.now(UTC):
        return None
    return token


async def authenticate(request: Request, db: AsyncSession) -> ScimClient:
    """The client of a SCIM request; 401/403/429 as SCIM errors. Commits the counters."""
    settings: Settings = request.app.state.settings
    secret = _bearer(request)
    token = await _find(db, secret) if secret else None
    if token is None:
        key = derive_key(settings.security, "rate-limit")
        hit = await rate_limit.hit(
            db, "scim-ip:" + keyed_digest(key, _client_ip(request)), _FAILED_WINDOW
        )
        await db.commit()
        log.info("scim_auth_failed", reason="missing" if secret is None else "invalid")
        if hit.count > settings.scim.failed_auth_per_ip:
            raise _too_many(hit)
        raise _unauthorized()
    hit = await rate_limit.hit(db, f"scim-token:{token.id}", _RATE_WINDOW)
    now = datetime.now(UTC)
    if token.last_used_at is None or now - token.last_used_at >= _TOUCH_INTERVAL:
        await db.execute(update(ScimToken).where(ScimToken.id == token.id).values(last_used_at=now))
    await db.commit()
    if hit.count > settings.scim.rate_limit_per_minute:
        log.warning("scim_rate_limited", token_id=token.id)
        raise _too_many(hit)
    if not (await get_config(db)).enabled:
        raise ScimError(403, "SCIM provisioning is disabled.")
    return ScimClient(token_id=token.id)


async def require_scim_client(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> ScimClient:
    return await authenticate(request, db)


ScimClientDep = Annotated[ScimClient, Depends(require_scim_client)]

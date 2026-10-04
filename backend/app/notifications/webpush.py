"""Sending one Web Push message to one device (RFC 8030 with VAPID and ``aes128gcm``).

Only HTTPS endpoints on the push services in ``OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ALLOWED_HOSTS``
are accepted: the endpoint comes from the browser, so it must not make the worker call
arbitrary (internal) hosts. Redirects are not followed. Results carry the HTTP status only,
never the endpoint (it is a capability URL) or the push service's response body.
"""

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.notifications.vapid import Vapid, encrypt

MAX_ENDPOINT_LENGTH = 2048
TIMEOUT_SECONDS = 10.0


class PushOutcome(enum.StrEnum):
    SENT = "sent"
    # The subscription expired or was revoked (404/410): delete it.
    GONE = "gone"
    # Rate limit, server error or no connection: worth trying again later.
    RETRY = "retry"
    # Anything else (400, 401/403 for a key mismatch, 413): trying again will not help.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PushResult:
    outcome: PushOutcome
    # HTTP status of the push service, ``None`` without a response.
    status: int | None = None


def endpoint_allowed(endpoint: str, allowed_hosts: Sequence[str]) -> bool:
    """``True`` for an HTTPS URL on one of the push service hosts (or a subdomain)."""
    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def push_service(endpoint: str) -> str:
    """Host of the push service (shown to the user, used in the data export)."""
    return (urlsplit(endpoint).hostname or "").lower()


def _classify(status: int) -> PushOutcome:
    if 200 <= status < 300:
        return PushOutcome.SENT
    if status in (404, 410):
        return PushOutcome.GONE
    if status == 429 or status >= 500:
        return PushOutcome.RETRY
    return PushOutcome.FAILED


async def send_push(
    http: httpx.AsyncClient,
    vapid: Vapid,
    subscription: dict[str, str],
    payload: bytes,
    *,
    ttl: int,
    allowed_hosts: Sequence[str],
) -> PushResult:
    """Encrypt ``payload`` for the device and hand it to its push service."""
    endpoint = subscription["endpoint"]
    if not endpoint_allowed(endpoint, allowed_hosts):
        return PushResult(PushOutcome.FAILED)
    body = encrypt(payload, subscription["p256dh"], subscription["auth"])
    parts = urlsplit(endpoint)
    headers = {
        "Authorization": vapid.authorization(f"https://{parts.netloc}"),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": "normal",
    }
    try:
        response = await http.post(endpoint, content=body, headers=headers)
    except httpx.HTTPError:
        return PushResult(PushOutcome.RETRY)
    return PushResult(_classify(response.status_code), response.status_code)


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)

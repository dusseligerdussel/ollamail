"""Sending one Web Push message to one device (RFC 8030 with VAPID and ``aes128gcm``).

Only HTTPS endpoints on the push services in ``OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ALLOWED_HOSTS``
are accepted: the endpoint comes from the browser, so it must not make the worker call
arbitrary (internal) hosts. Redirects are not followed. Results carry the HTTP status only,
never the endpoint (it is a capability URL) or the push service's response body.

A worker process sends through one shared client (``push_client``, connections are reused)
and keeps a circuit breaker per push service (``PushServiceBreaker``): after several failed
attempts in a row, devices on that service are put off without a request for a while, so a
blocked or hanging service costs one connect timeout per attempt, not one per device.
"""

import asyncio
import enum
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.notifications.vapid import Vapid, encrypt

MAX_ENDPOINT_LENGTH = 2048
# Typical self-hosted setups block outgoing traffic: give up connecting quickly.
TIMEOUT = httpx.Timeout(10.0, connect=3.0)
# Failed attempts in a row after which a push service is skipped, and for how long.
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECONDS = 60.0


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


class PushServiceBreaker:
    """Circuit breaker per push service host, kept for the life of a worker process.

    ``BREAKER_THRESHOLD`` results worth retrying in a row (no connection, timeout, 429,
    5xx) open it for ``BREAKER_COOLDOWN_SECONDS``; then one attempt at a time gets through
    again, and a delivered message closes it."""

    def __init__(
        self,
        threshold: int = BREAKER_THRESHOLD,
        cooldown: float = BREAKER_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def allows(self, host: str) -> bool:
        until = self._open_until.get(host)
        if until is None:
            return True
        if self._clock() < until:
            return False
        # Half open: let this attempt through, keep the others out until it is answered.
        self._open_until[host] = self._clock() + self._cooldown
        return True

    def record(self, host: str, outcome: PushOutcome) -> None:
        if outcome is not PushOutcome.RETRY:
            # The service answered: it is reachable.
            self._failures.pop(host, None)
            self._open_until.pop(host, None)
            return
        failures = self._failures.get(host, 0) + 1
        self._failures[host] = failures
        if failures >= self._threshold:
            self._open_until[host] = self._clock() + self._cooldown


async def send_push(
    http: httpx.AsyncClient,
    vapid: Vapid,
    subscription: dict[str, str],
    payload: bytes,
    *,
    ttl: int,
    allowed_hosts: Sequence[str],
    breaker: PushServiceBreaker | None = None,
) -> PushResult:
    """Encrypt ``payload`` for the device and hand it to its push service."""
    endpoint = subscription["endpoint"]
    if not endpoint_allowed(endpoint, allowed_hosts):
        return PushResult(PushOutcome.FAILED)
    host = push_service(endpoint)
    if breaker is not None and not breaker.allows(host):
        return PushResult(PushOutcome.RETRY)
    result = await _post(http, vapid, subscription, payload, ttl=ttl)
    if breaker is not None:
        breaker.record(host, result.outcome)
    return result


async def _post(
    http: httpx.AsyncClient,
    vapid: Vapid,
    subscription: dict[str, str],
    payload: bytes,
    *,
    ttl: int,
) -> PushResult:
    endpoint = subscription["endpoint"]
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
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)


# One client and one breaker per worker process. The client belongs to the event loop it
# was created in (tests run several loops), so another loop gets a new one.
_client: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None
_breaker = PushServiceBreaker()


def push_client() -> httpx.AsyncClient:
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop or _client[1].is_closed:
        _client = (loop, http_client())
    return _client[1]


def push_breaker() -> PushServiceBreaker:
    return _breaker


async def close_push_client() -> None:
    """Close the shared client (worker shutdown)."""
    global _client
    if _client is not None:
        loop, client = _client
        _client = None
        if loop is asyncio.get_running_loop():
            await client.aclose()

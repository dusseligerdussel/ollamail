"""Small async client for Google REST APIs (Gmail, Pub/Sub) on top of ``httpx``.

* Adds the bearer token; after a 401 the token is refreshed once.
* Retries rate limits (429, 403 ``rateLimitExceeded``/``userRateLimitExceeded``), server
  errors (5xx) and network errors with exponential backoff, honouring ``Retry-After``.
* ``batch`` sends up to ``MAX_BATCH`` GET requests as one multipart batch request
  (``/batch/gmail/v1``); failed parts that can be retried are sent again.
* Errors become ``ProviderError``s with codes only. Response bodies (which may contain mail
  data) and Google's error texts are never put into exceptions or logs.
"""

import asyncio
import json
import random
import secrets
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from app.mail.providers.base import (
    AuthenticationError,
    ConfigurationError,
    ConnectionFailedError,
    ProviderError,
)
from app.mail.providers.gmail_auth import TokenSource

GOOGLE_API = "https://gmail.googleapis.com"
BATCH_PATH = "/batch/gmail/v1"
PUBSUB_API = "https://pubsub.googleapis.com/v1"
# Google allows 100 parts per batch and recommends at most 50 for Gmail.
MAX_BATCH = 50
MAX_ATTEMPTS = 5
MAX_DELAY = 32.0

_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"})
_SCOPE_REASONS = frozenset(
    {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "forbidden"}
)
_DISABLED_REASONS = frozenset({"accessNotConfigured", "SERVICE_DISABLED"})

Sleep = Callable[[float], Awaitable[None]]


class NotFoundError(ProviderError):
    code = "not_found"


class BadRequestError(ProviderError):
    code = "bad_request"


class ConflictError(ProviderError):
    code = "conflict"


def error_reason(data: Any) -> str | None:
    """The machine-readable reason of a Google error body (v1 ``errors[].reason`` or the
    ``ErrorInfo`` detail of newer APIs)."""
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    for item in error.get("errors") or ():
        if isinstance(item, dict) and isinstance(item.get("reason"), str):
            return str(item["reason"])
    for detail in error.get("details") or ():
        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
            return str(detail["reason"])
    status = error.get("status")
    return status if isinstance(status, str) else None


def _retryable(status: int, reason: str | None) -> bool:
    return status == 429 or status >= 500 or (status == 403 and reason in _RATE_REASONS)


def raise_for_status(status: int, data: Any) -> None:
    """Translate an error response into a ``ProviderError`` (no server texts)."""
    if status < 400:
        return
    reason = error_reason(data)
    if status == 401:
        raise AuthenticationError()
    if status == 403:
        if reason in _DISABLED_REASONS:
            raise ConfigurationError(code="api_disabled")
        if reason in _RATE_REASONS:
            raise ConnectionFailedError(code="rate_limited")
        if reason in _SCOPE_REASONS:
            raise AuthenticationError(code="insufficient_scope")
        raise AuthenticationError(code="access_denied")
    if status == 404:
        raise NotFoundError()
    if status in (409, 412):
        # 412: an ``If-Match`` precondition failed (Google Tasks).
        raise ConflictError()
    if status == 429:
        raise ConnectionFailedError(code="rate_limited")
    if status >= 500:
        raise ConnectionFailedError(code="server_error")
    raise BadRequestError()


def _retry_after(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    try:
        return min(float(value), 60.0) if value is not None else None
    except ValueError:
        return None


def _delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return min(2.0**attempt, MAX_DELAY) + random.uniform(0, 1)


def _json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return None


class GoogleApiClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: TokenSource,
        base_url: str,
        *,
        host: str = GOOGLE_API,
        sleep: Sleep = asyncio.sleep,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        """``base_url`` is prepended to request paths; ``host`` serves batch requests."""
        self._http = http
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._host = host.rstrip("/")
        self._sleep = sleep
        self._max_attempts = max_attempts

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
        retry: bool = True,
    ) -> httpx.Response:
        """Send with token handling and retries; returns the final response (which may
        still be an error that is not worth retrying). ``retry=False`` (sending mail) only
        repeats after a token refresh, so a request that may have taken effect is not
        sent twice."""
        refreshed = force = False
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            token = await self._tokens.token(refresh=force)
            force = False
            request_headers = {"Authorization": f"Bearer {token}", **(headers or {})}
            extra: dict[str, Any] = {} if request_timeout is None else {"timeout": request_timeout}
            try:
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    content=content,
                    headers=request_headers,
                    **extra,
                )
            except httpx.HTTPError:
                response = None
                if not retry or attempt + 1 >= self._max_attempts:
                    raise ConnectionFailedError() from None
                await self._sleep(_delay(attempt, None))
                continue
            if response.status_code == 401 and not refreshed:
                refreshed = force = True
                continue
            retryable = _retryable(response.status_code, error_reason(_json(response)))
            if retry and retryable and attempt + 1 < self._max_attempts:
                await self._sleep(_delay(attempt, _retry_after(response)))
                continue
            return response
        assert response is not None
        return response

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{self._base_url}{path}"
        response = await self._send(
            method,
            url,
            params=params,
            json_body=json,
            headers=headers,
            request_timeout=request_timeout,
            retry=retry,
        )
        data = _json(response)
        raise_for_status(response.status_code, data)
        if not isinstance(data, dict):
            raise ProviderError(code="invalid_response")
        return data

    async def batch(self, paths: Sequence[str]) -> list[tuple[int, Any]]:
        """GET ``paths`` (absolute paths on ``host``, e.g. ``/gmail/v1/users/me/...``) in
        multipart batches. Returns ``(status, json)`` per path, in order. Parts that hit a
        rate limit or server error are retried; other errors are returned to the caller."""
        results: dict[int, tuple[int, Any]] = {}
        for start in range(0, len(paths), MAX_BATCH):
            chunk = list(range(start, min(start + MAX_BATCH, len(paths))))
            await self._batch_chunk(paths, chunk, results)
        return [results[index] for index in range(len(paths))]

    async def _batch_chunk(
        self, paths: Sequence[str], pending: list[int], results: dict[int, tuple[int, Any]]
    ) -> None:
        refreshed = False
        for attempt in range(self._max_attempts):
            boundary = f"batch_{secrets.token_hex(12)}"
            body = _batch_body(boundary, [(index, paths[index]) for index in pending])
            response = await self._send(
                "POST",
                f"{self._host}{BATCH_PATH}",
                content=body,
                headers={"Content-Type": f"multipart/mixed; boundary={boundary}"},
            )
            if response.status_code >= 400:
                raise_for_status(response.status_code, _json(response))
            parts = parse_batch_response(response)
            retry: list[int] = []
            unauthorized = False
            for index in pending:
                status, data = parts.get(index, (500, None))
                if status == 401 and not refreshed:
                    retry.append(index)
                    unauthorized = True
                elif _retryable(status, error_reason(data)):
                    retry.append(index)
                else:
                    results[index] = (status, data)
            if not retry:
                return
            if unauthorized:
                await self._tokens.token(refresh=True)
                refreshed = True
            pending = retry
            if attempt + 1 < self._max_attempts:
                await self._sleep(_delay(attempt, None))
        status, data = parts.get(pending[0], (500, None))
        raise_for_status(status if status >= 400 else 500, data)


def _batch_body(boundary: str, requests: list[tuple[int, str]]) -> bytes:
    lines: list[str] = []
    for index, path in requests:
        lines += [
            f"--{boundary}",
            "Content-Type: application/http",
            f"Content-ID: <item{index}>",
            "",
            f"GET {path}",
            "",
        ]
    lines.append(f"--{boundary}--")
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _boundary(content_type: str) -> str | None:
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary":
            return value.strip('"')
    return None


def parse_batch_response(response: httpx.Response) -> dict[int, tuple[int, Any]]:
    """Parse a ``multipart/mixed`` batch response into ``{index: (status, json)}``."""
    boundary = _boundary(response.headers.get("Content-Type", ""))
    if not boundary:
        raise ProviderError(code="invalid_response")
    text = response.content.decode("utf-8", "replace")
    results: dict[int, tuple[int, Any]] = {}
    for part in text.split(f"--{boundary}"):
        part = part.strip("\r\n")
        if not part or part.startswith("--"):
            continue
        outer, _, inner = _split_headers(part)
        index = _content_index(outer)
        status_line, _, rest = inner.partition("\n")
        _, _, body = _split_headers(rest)
        fields = status_line.split()
        if index is None or len(fields) < 2 or not fields[1].isdigit():
            continue
        body = body.strip()
        try:
            data: Any = json.loads(body) if body else {}
        except ValueError:
            data = None
        results[index] = (int(fields[1]), data)
    return results


def _split_headers(block: str) -> tuple[str, str, str]:
    block = block.replace("\r\n", "\n").lstrip("\n")
    return block.partition("\n\n")


def _content_index(headers: str) -> int | None:
    for line in headers.split("\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-id":
            # Google answers ``<response-item7>`` for the request ``<item7>``.
            digits = value.strip().strip("<>").rpartition("item")[2]
            return int(digits) if digits.isdigit() else None
    return None

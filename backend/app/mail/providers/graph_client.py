"""Small Microsoft Graph HTTP client for the mail provider (docs/providers/microsoft365.md).

* Every request carries ``Authorization`` and ``Prefer: IdType="ImmutableId"`` (message IDs
  stay the same when a message is moved). On 401 the token is refreshed once.
* Throttling: 429/503/504 (and 500/502) are retried after ``Retry-After`` (at most
  ``MAX_WAIT`` seconds per attempt, exponential without the header), up to
  ``max_retries`` times; then ``ConnectionFailedError`` (``throttled``/``server_error``).
* Errors are mapped to the provider error classes. Graph error messages are never put
  into exceptions or logs, only the static error code of this module.
* ``batch`` sends up to 20 requests per JSON batch and retries throttled parts.
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger
from app.mail.providers.base import (
    AuthenticationError,
    ConnectionFailedError,
    CursorInvalidError,
    ProviderError,
)
from app.mail.providers.graph_auth import TokenGetter

log = get_logger(__name__)

BATCH_LIMIT = 20
MAX_WAIT = 120.0
IMMUTABLE_IDS = 'IdType="ImmutableId"'
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Delta tokens that the server no longer accepts.
_CURSOR_INVALID = frozenset(
    {"syncstatenotfound", "syncstateinvalid", "resyncrequired", "errorinvalidsyncstatedata"}
)

Sleep = Callable[[float], Awaitable[None]]


class GraphNotFoundError(ProviderError):
    """404 (or a malformed ID). Callers turn it into ``MessageNotFoundError`` etc."""

    code = "not_found"


@dataclass(frozen=True, slots=True)
class BatchRequest:
    id: str
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None


@dataclass(frozen=True, slots=True)
class BatchResponse:
    id: str
    status: int
    headers: Mapping[str, str]
    body: Any

    def content(self) -> bytes:
        """Binary body (``$value``): JSON batching returns it base64-encoded."""
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            try:
                return base64.b64decode(self.body, validate=True)
            except (binascii.Error, ValueError):
                return self.body.encode("utf-8")
        raise ProviderError(code="invalid_response")

    def json(self) -> dict[str, Any]:
        if isinstance(self.body, dict):
            return self.body
        raise ProviderError(code="invalid_response")

    def error_code(self) -> str:
        return _error_code(self.body)


def _error_code(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return str(error["code"])
    return ""


def _retry_after(headers: Mapping[str, str], attempt: int) -> float:
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        seconds = float(value) if value is not None else 2.0**attempt
    except ValueError:
        seconds = 2.0**attempt
    return max(0.0, min(seconds, MAX_WAIT))


def error_for(status: int, body: Any) -> ProviderError:
    """Provider error for a failed Graph response (no server text in the exception)."""
    code = _error_code(body).lower()
    if code in _CURSOR_INVALID or status == 410:
        return CursorInvalidError()
    if status == 401:
        return AuthenticationError()
    if status == 403:
        return AuthenticationError(code="access_denied")
    if status == 404 or code in {"errorinvalidid", "errorinvalididmalformed", "erroritemnotfound"}:
        return GraphNotFoundError()
    if status in {429, 503, 504}:
        return ConnectionFailedError(code="throttled")
    if status >= 500:
        return ConnectionFailedError(code="server_error")
    return ProviderError(code="request_failed")


class GraphClient:
    def __init__(
        self,
        *,
        api_url: str,
        token: TokenGetter,
        timeout: float,
        max_retries: int,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def url(self, path: str) -> str:
        if not path.startswith(("https://", "http://")):
            return f"{self.api_url}{path}"
        # Paging/delta links come from the server; the token is only ever sent to Graph.
        if httpx.URL(path).host != httpx.URL(self.api_url).host:
            raise ProviderError(code="invalid_response")
        return path

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        prefer: Sequence[str] = (),
    ) -> httpx.Response:
        """Send a request; returns the successful response or raises a provider error."""
        refreshed = False
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {await self._token(False)}",
                "Prefer": ", ".join((IMMUTABLE_IDS, *prefer)),
            }
            try:
                response = await self._http.request(
                    method, self.url(path), params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                log.warning("graph_request_failed", error_type=type(exc).__name__)
                raise ConnectionFailedError() from None
            if response.is_success:
                return response
            body = _json_or_none(response)
            if response.status_code == 401 and not refreshed:
                refreshed = True
                await self._token(True)
                continue
            if response.status_code in _RETRY_STATUS and attempt < self._max_retries:
                wait = _retry_after(response.headers, attempt)
                log.info("graph_throttled", status=response.status_code, wait=wait)
                attempt += 1
                await self._sleep(wait)
                continue
            raise error_for(response.status_code, body)

    async def get_json(
        self, path: str, *, params: Mapping[str, str] | None = None, prefer: Sequence[str] = ()
    ) -> dict[str, Any]:
        response = await self.request("GET", path, params=params, prefer=prefer)
        data = _json_or_none(response)
        if not isinstance(data, dict):
            raise ProviderError(code="invalid_response")
        return data

    async def items(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """All items of a collection, following ``@odata.nextLink``."""
        page = await self.get_json(path, params=params)
        while True:
            for item in page.get("value", []):
                if isinstance(item, dict):
                    yield item
            next_link = page.get("@odata.nextLink")
            if not isinstance(next_link, str):
                return
            page = await self.get_json(next_link)

    async def batch(self, requests: Sequence[BatchRequest]) -> dict[str, BatchResponse]:
        """Run requests as JSON batches; throttled parts are retried. Returns responses by
        request ID (also failed ones, callers check ``status``)."""
        results: dict[str, BatchResponse] = {}
        for start in range(0, len(requests), BATCH_LIMIT):
            pending = list(requests[start : start + BATCH_LIMIT])
            attempt = 0
            while pending:
                responses = await self._batch_once(pending)
                retry: list[BatchRequest] = []
                wait = 0.0
                for request in pending:
                    response = responses.get(request.id)
                    if response is None:
                        raise ProviderError(code="invalid_response")
                    if response.status in _RETRY_STATUS and attempt < self._max_retries:
                        retry.append(request)
                        wait = max(wait, _retry_after(response.headers, attempt))
                        continue
                    results[request.id] = response
                if retry:
                    log.info("graph_batch_throttled", requests=len(retry), wait=wait)
                    attempt += 1
                    await self._sleep(wait)
                pending = retry
        return results

    async def _batch_once(self, requests: Sequence[BatchRequest]) -> dict[str, BatchResponse]:
        payload = {
            "requests": [
                {
                    "id": request.id,
                    "method": request.method,
                    "url": request.url,
                    "headers": {"Prefer": IMMUTABLE_IDS, **request.headers},
                    **({"body": request.body} if request.body is not None else {}),
                }
                for request in requests
            ]
        }
        response = await self.request("POST", "/$batch", json_body=payload)
        data = _json_or_none(response)
        if not isinstance(data, dict) or not isinstance(data.get("responses"), list):
            raise ProviderError(code="invalid_response")
        results = {}
        for item in data["responses"]:
            if not isinstance(item, dict) or "id" not in item:
                continue
            headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
            results[str(item["id"])] = BatchResponse(
                id=str(item["id"]),
                status=int(item.get("status", 0)),
                headers={str(k): str(v) for k, v in (headers or {}).items()},
                body=item.get("body"),
            )
        return results


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

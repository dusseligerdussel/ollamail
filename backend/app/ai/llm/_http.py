"""httpx helpers shared by the providers."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx

from app.ai.llm.errors import LLMRequestError, LLMUnavailableError, ModelNotAvailableError


@contextmanager
def transport_errors() -> Iterator[None]:
    """Map transport failures to :class:`LLMUnavailableError` without request details."""
    try:
        yield
    except httpx.TimeoutException as exc:
        raise LLMUnavailableError("LLM request timed out") from exc
    except httpx.TransportError as exc:
        raise LLMUnavailableError("LLM endpoint unreachable") from exc


def raise_for_status(response: httpx.Response, *, model: str | None = None) -> None:
    """Raise a typed error for non-2xx responses. The body is never included: some
    servers echo the request (and thereby mail content) in error messages."""
    status = response.status_code
    if status < 400:
        return
    if status == 404 and model is not None:
        raise ModelNotAvailableError(model)
    if status >= 500 or status == 429:
        raise LLMUnavailableError(f"LLM endpoint returned HTTP {status}")
    raise LLMRequestError(f"LLM endpoint returned HTTP {status}", status_code=status)


def json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMUnavailableError("LLM endpoint returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise LLMUnavailableError("LLM endpoint returned an unexpected response")
    return data


@asynccontextmanager
async def stream_request(
    client: httpx.AsyncClient, url: str, payload: dict[str, Any], *, model: str
) -> AsyncIterator[httpx.Response]:
    with transport_errors():
        async with client.stream("POST", url, json=payload) as response:
            if response.status_code >= 400:
                await response.aread()
                raise_for_status(response, model=model)
            yield response


def build_client(
    base_url: str, *, timeout: float, headers: dict[str, str] | None = None
) -> httpx.AsyncClient:
    # Relative request paths ("api/chat") resolve below base_url, keeping e.g. "/v1".
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/") + "/",
        timeout=httpx.Timeout(timeout, connect=10.0),
        headers=headers,
    )

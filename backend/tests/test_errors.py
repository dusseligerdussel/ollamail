import io
import json

from fastapi import FastAPI, HTTPException
from httpx import AsyncClient
from pydantic import BaseModel

from app.core.config import LoggingSettings, Settings
from app.core.errors import PROBLEM_MEDIA_TYPE, ProblemError
from app.core.logging import configure_logging
from app.main import create_app
from tests.conftest import api_client

PII = "alice@example.com"


class Payload(BaseModel):
    count: int


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        if item_id == "teapot":
            raise ProblemError(418, detail="Short and stout.", type="/problems/teapot", hint="x")
        if item_id == "forbidden":
            raise HTTPException(status_code=403, detail="Mailbox not shared with you.")
        raise RuntimeError(f"lookup failed for {PII}")

    @app.post("/payload")
    async def payload(body: Payload) -> dict[str, int]:
        return {"count": body.count}

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return api_client(app)


async def test_not_found_is_problem_details(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "request_id": response.headers["x-request-id"],
    }


async def test_http_exception_keeps_detail(settings: Settings) -> None:
    async with await _client(_app(settings)) as client:
        response = await client.get("/items/forbidden")

    assert response.status_code == 403
    assert response.json()["title"] == "Forbidden"
    assert response.json()["detail"] == "Mailbox not shared with you."


async def test_problem_error_with_extensions(settings: Settings) -> None:
    async with await _client(_app(settings)) as client:
        response = await client.get("/items/teapot")

    assert response.status_code == 418
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "/problems/teapot"
    assert body["title"] == "I'm a Teapot"
    assert body["detail"] == "Short and stout."
    assert body["hint"] == "x"


async def test_validation_error_does_not_echo_input(settings: Settings) -> None:
    async with await _client(_app(settings)) as client:
        response = await client.post("/payload", json={"count": PII})

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert PII not in response.text
    body = response.json()
    assert body["errors"][0]["loc"] == ["body", "count"]
    assert set(body["errors"][0]) == {"loc", "msg", "type"}


async def test_unhandled_exception_is_500_problem_without_pii(settings: Settings) -> None:
    app = _app(settings)
    stream = io.StringIO()
    configure_logging(LoggingSettings(format="json"), stream=stream)

    async with await _client(app) as client:
        response = await client.get("/items/boom", headers={"X-Request-ID": "req-123"})

    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["x-request-id"] == "req-123"
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "request_id": "req-123",
    }
    assert PII not in response.text
    assert PII not in stream.getvalue()
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    error = next(r for r in records if r["event"] == "unhandled_exception")
    assert error["request_id"] == "req-123"
    assert error["exception"][0]["exc_type"] == "RuntimeError"


async def test_request_id_is_generated_when_missing_or_invalid(client: AsyncClient) -> None:
    generated = await client.get("/healthz")
    invalid = await client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
    provided = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})

    assert len(generated.headers["x-request-id"]) == 32
    assert invalid.headers["x-request-id"] not in {"bad id\twith spaces", ""}
    assert provided.headers["x-request-id"] == "abc-123"


async def test_request_log_uses_route_template(settings: Settings) -> None:
    app = _app(settings)
    stream = io.StringIO()
    configure_logging(LoggingSettings(format="json"), stream=stream)

    async with await _client(app) as client:
        await client.get("/items/forbidden?q=alice@example.com")

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    [finished] = [r for r in records if r["event"] == "request_finished"]
    assert finished["route"] == "/items/{item_id}"
    assert finished["status_code"] == 403
    assert finished["method"] == "GET"
    assert "request_id" in finished
    assert "alice" not in stream.getvalue()

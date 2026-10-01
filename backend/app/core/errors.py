"""Uniform error responses as RFC 9457 Problem Details (``application/problem+json``)."""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemDetails(BaseModel):
    """RFC 9457 problem object; extension members are allowed."""

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    request_id: str | None = None


class ProblemError(Exception):
    """Raise from application code to return a Problem Details response.

    ``detail`` and extension members are sent to the client but never logged; they must
    still not contain other users' data.
    """

    def __init__(
        self,
        status: int,
        *,
        title: str | None = None,
        detail: str | None = None,
        type: str = "about:blank",
        **extensions: Any,
    ) -> None:
        super().__init__(title or HTTPStatus(status).phrase)
        self.status = status
        self.title = title or HTTPStatus(status).phrase
        self.detail = detail
        self.type = type
        self.extensions = extensions


def problem_response(
    status: int,
    *,
    title: str | None = None,
    detail: str | None = None,
    type: str = "about:blank",
    headers: Mapping[str, str] | None = None,
    **extensions: Any,
) -> JSONResponse:
    problem = ProblemDetails(
        type=type,
        title=title or HTTPStatus(status).phrase,
        status=status,
        detail=detail,
        request_id=structlog.contextvars.get_contextvars().get("request_id"),
        **extensions,
    )
    return JSONResponse(
        problem.model_dump(exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


async def _handle_problem(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return problem_response(
        exc.status, title=exc.title, detail=exc.detail, type=exc.type, **exc.extensions
    )


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    title = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    return problem_response(exc.status_code, detail=detail, headers=exc.headers)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Only location, message and type: the rejected input itself may be personal data.
    errors = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]
    return problem_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        detail="The request is invalid.",
        errors=errors,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register Problem Details handlers. Unhandled exceptions (500) are converted by
    ``RequestContextMiddleware`` so the response still carries the request ID."""
    app.add_exception_handler(ProblemError, _handle_problem)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)

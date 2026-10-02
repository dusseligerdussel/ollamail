"""SCIM error responses (RFC 7644 §3.12) for every failure on a SCIM route.

IdPs parse ``status``/``scimType``/``detail`` from the SCIM error schema, not Problem Details,
so ``ScimRoute`` turns ``ScimError``, ``ProblemError`` (e.g. last admin, admin lockout) and
request validation errors into SCIM errors. Details are static texts: they never echo
attribute values (names, addresses) back into responses that IdPs log.
"""

from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ProblemError

SCIM_MEDIA_TYPE = "application/scim+json"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


class ScimError(Exception):
    """A SCIM error; ``scim_type`` is one of the codes of RFC 7644 Table 9."""

    def __init__(
        self,
        status: int,
        detail: str,
        scim_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.headers = dict(headers or {})


def invalid_value(detail: str) -> ScimError:
    return ScimError(400, detail, "invalidValue")


def not_found(resource: str) -> ScimError:
    return ScimError(404, f"{resource} not found.")


def scim_response(
    content: Any, status_code: int = 200, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        content, status_code=status_code, media_type=SCIM_MEDIA_TYPE, headers=headers
    )


def error_response(error: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(error.status),
        "detail": error.detail,
    }
    if error.scim_type:
        body["scimType"] = error.scim_type
    return scim_response(body, error.status, error.headers)


def _from_problem(problem: ProblemError) -> ScimError:
    scim_type = "uniqueness" if problem.status == 409 else None
    return ScimError(problem.status, problem.detail or problem.title, scim_type)


class ScimRoute(APIRoute):
    """Route class of the SCIM router: all errors in SCIM format."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def scim_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except ScimError as error:
                return error_response(error)
            except ProblemError as problem:
                return error_response(_from_problem(problem))
            except RequestValidationError:
                return error_response(invalid_value("The request is not valid."))
            except StarletteHTTPException as exc:
                return error_response(ScimError(exc.status_code, str(exc.detail)))

        return scim_handler

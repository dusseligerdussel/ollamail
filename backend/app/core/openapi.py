"""OpenAPI helpers: stable operation IDs and a deterministic schema export.

The frontend generates its typed API client from the exported schema
(``frontend/src/api/openapi.json``), so the output must not change between runs.
"""

import json
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


def generate_operation_id(route: APIRoute) -> str:
    """``<first tag>_<endpoint function name>``, e.g. ``health_healthz``.

    FastAPI's default (``healthz_healthz_get``) also encodes path and method, so it
    changes whenever a route moves. Uniqueness is checked by ``openapi_schema``.
    """
    if route.tags:
        return f"{route.tags[0]}_{route.name}"
    return route.name


def openapi_schema(app: FastAPI) -> dict[str, Any]:
    """The app's OpenAPI schema; raises if two operations share an ID."""
    schema = app.openapi()
    seen: dict[str, str] = {}
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            operation_id = operation.get("operationId")
            if operation_id is None:
                continue
            where = f"{method.upper()} {path}"
            if operation_id in seen:
                raise ValueError(
                    f"duplicate operationId {operation_id!r}: {seen[operation_id]} and {where}"
                )
            seen[operation_id] = where
    return schema


def render_openapi(app: FastAPI) -> str:
    """Schema as JSON text with sorted keys and a trailing newline."""
    return json.dumps(openapi_schema(app), indent=2, sort_keys=True, ensure_ascii=False) + "\n"

import json
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from app.core.openapi import generate_operation_id, render_openapi
from app.main import create_app
from scripts import export_openapi


def test_operation_ids_are_stable_names() -> None:
    schema = json.loads(render_openapi(create_app()))

    assert schema["paths"]["/healthz"]["get"]["operationId"] == "health_healthz"
    assert schema["paths"]["/readyz"]["get"]["operationId"] == "health_readyz"


def test_render_is_deterministic() -> None:
    assert render_openapi(create_app()) == render_openapi(create_app())


def test_duplicate_operation_ids_are_rejected() -> None:
    app = FastAPI(generate_unique_id_function=generate_operation_id)
    first, second = APIRouter(tags=["mail"]), APIRouter(tags=["mail"])

    @first.get("/a")
    async def items() -> None: ...

    @second.get("/b", name="items")
    async def other() -> None: ...

    app.include_router(first)
    app.include_router(second)

    # FastAPI itself only warns about the duplicate.
    with (
        pytest.warns(UserWarning, match="Duplicate Operation ID"),
        pytest.raises(ValueError, match="duplicate operationId 'mail_items'"),
    ):
        render_openapi(app)


def test_operation_id_without_tag_is_the_function_name() -> None:
    app = FastAPI(generate_unique_id_function=generate_operation_id)

    @app.get("/ping")
    async def ping() -> None: ...

    schema = json.loads(render_openapi(app))
    assert schema["paths"]["/ping"]["get"]["operationId"] == "ping"


def test_export_script_writes_schema(tmp_path: Path) -> None:
    target = tmp_path / "api" / "openapi.json"

    assert export_openapi.main([str(target)]) == 0

    assert target.read_text(encoding="utf-8") == render_openapi(create_app())


def test_export_script_writes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    assert export_openapi.main(["-"]) == 0

    assert json.loads(capsys.readouterr().out)["info"]["title"] == "ollamail"

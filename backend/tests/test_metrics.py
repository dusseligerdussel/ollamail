"""Token check, LLM call metrics and the worker's metrics endpoint (app/core/metrics.py)."""

import socket
import urllib.error
import urllib.request
from dataclasses import replace

import pytest
from prometheus_client import REGISTRY
from pydantic import SecretStr

from app.ai.llm.metrics import LLMCallMetrics, observe
from app.core.config import MetricsSettings
from app.core.metrics import authorized, start_worker_metrics_server

TOKEN = SecretStr("s3cret-token")


@pytest.mark.parametrize(
    ("header", "token", "expected"),
    [
        (None, None, True),
        ("Bearer anything", None, True),
        ("Bearer s3cret-token", TOKEN, True),
        ("bearer s3cret-token", TOKEN, True),
        (None, TOKEN, False),
        ("Bearer", TOKEN, False),
        ("Bearer wrong", TOKEN, False),
        ("Basic s3cret-token", TOKEN, False),
    ],
)
def test_authorized(header: str | None, token: SecretStr | None, expected: bool) -> None:
    assert authorized(header, token) is expected


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_llm_calls_are_counted_without_content() -> None:
    labels = {
        "task": "summary",
        "operation": "complete",
        "endpoint": "gpu",
        "provider": "ollama",
        "model": "metrics-test:1b",
    }
    before = _sample("ollamail_llm_request_duration_seconds_count", **labels, outcome="success")
    call = LLMCallMetrics(
        task="summary",
        operation="complete",
        endpoint="gpu",
        provider="ollama",
        model="metrics-test:1b",
        prompt_version="summary-v1",
        duration_ms=4000,
        success=True,
        prompt_tokens=1000,
        completion_tokens=200,
    )

    observe(call)
    observe(replace(call, duration_ms=30000, success=False, error_type="LLMTimeoutError"))

    assert (
        _sample("ollamail_llm_request_duration_seconds_count", **labels, outcome="success")
        == before + 1
    )
    assert _sample("ollamail_llm_tokens_total", **labels, kind="completion") >= 200
    assert _sample("ollamail_llm_tokens_total", **labels, kind="prompt") >= 1000
    # 200 tokens in 4 s: 50 tokens per second.
    rate = {"endpoint": "gpu", "provider": "ollama", "model": "metrics-test:1b"}
    assert _sample("ollamail_llm_completion_tokens_per_second_sum", **rate) >= 50
    assert _sample("ollamail_llm_errors_total", **labels, error_type="LLMTimeoutError") >= 1


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _get(url: str, token: str | None = None) -> tuple[int, str]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def test_worker_metrics_server_serves_metrics_with_token() -> None:
    port = _free_port()
    settings = MetricsSettings(enabled=True, worker_port=port, token=TOKEN)
    server = start_worker_metrics_server(settings, host="127.0.0.1")
    assert server is not None
    try:
        base = f"http://127.0.0.1:{port}"
        status, body = _get(f"{base}/metrics", "s3cret-token")
        assert status == 200
        assert "python_info" in body
        assert _get(f"{base}/metrics")[0] == 401
        assert _get(f"{base}/metrics", "wrong")[0] == 401
        assert _get(f"{base}/other", "s3cret-token")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "settings",
    [MetricsSettings(enabled=False), MetricsSettings(enabled=True, worker_port=0)],
)
def test_worker_metrics_server_is_optional(settings: MetricsSettings) -> None:
    assert start_worker_metrics_server(settings) is None

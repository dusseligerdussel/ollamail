"""Prometheus metrics (``OLLAMAIL_METRICS_*``, off by default): token check and the
worker's endpoint.

Process metrics live in the default registry: LLM latency, tokens and errors
(``app.ai.llm.metrics``) plus Python/process metrics. Each process counts its own: the api
serves them on ``/metrics`` together with the database metrics (``app.admin.metrics``),
every worker on ``:OLLAMAIL_METRICS_WORKER_PORT/metrics``.

Privacy (docs/PRIVACY.md): labels hold IDs, codes and configuration values only, never mail
content, subjects, addresses or display names.
"""

import hmac
import threading
from collections.abc import Callable, Iterable
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import REGISTRY, make_wsgi_app
from pydantic import SecretStr

from app.core.config import MetricsSettings
from app.core.logging import get_logger

log = get_logger(__name__)


def authorized(header: str | None, token: SecretStr | None) -> bool:
    """``True`` without a configured token, else only for ``Bearer <token>``."""
    if token is None:
        return True
    scheme, _, value = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return hmac.compare_digest(value.strip().encode(), token.get_secret_value().encode())


# -- worker endpoint ----------------------------------------------------------------------


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """No access log on stderr (unstructured, and not needed)."""


def metrics_wsgi_app(token: SecretStr | None) -> Callable[..., Iterable[bytes]]:
    """``/metrics`` of the default registry, with the same token check as the api."""
    serve = make_wsgi_app(REGISTRY)

    def app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics":
            start_response("404 Not Found", [("Content-Type", "text/plain")])
            return [b"not found\n"]
        if not authorized(environ.get("HTTP_AUTHORIZATION"), token):
            start_response(
                "401 Unauthorized",
                [("Content-Type", "text/plain"), ("WWW-Authenticate", "Bearer")],
            )
            return [b"unauthorized\n"]
        body: Iterable[bytes] = serve(environ, start_response)
        return body

    return app


def start_worker_metrics_server(
    settings: MetricsSettings, host: str = "0.0.0.0"
) -> WSGIServer | None:
    """Serve the worker's metrics in a daemon thread; ``None`` if disabled.

    A thread instead of the event loop: scrapes still answer while jobs keep the loop busy.
    Stop it with ``server.shutdown()``.
    """
    if not settings.enabled or not settings.worker_port:
        return None
    server = make_server(
        host,
        settings.worker_port,
        metrics_wsgi_app(settings.token),
        handler_class=_QuietHandler,
    )
    threading.Thread(target=server.serve_forever, name="metrics", daemon=True).start()
    log.info("worker_metrics_started", port=settings.worker_port)
    return server

"""Process-wide resolver and gateway for worker jobs and CLI commands.

The API builds its own in ``app.main`` (``app.state.llm``); worker modules call
``worker_gateway()`` instead of constructing a gateway, so all jobs of a process share
one settings cache, one change listener and one limit on parallel LLM requests.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.ai.llm.gateway import LLMGateway
from app.ai.settings.resolver import DbConfigResolver
from app.core.config import Settings, get_settings
from app.core.db import Database, libpq_url

_resolver: DbConfigResolver | None = None
_gateway: LLMGateway | None = None


def build_resolver(
    settings: Settings, database: Database, *, autostart: bool = False
) -> DbConfigResolver:
    return DbConfigResolver(
        settings.llm,
        database.sessionmaker,
        dsn=libpq_url(settings.database),
        connect_timeout=settings.database.connect_timeout,
        autostart=autostart,
    )


def worker_resolver() -> DbConfigResolver:
    global _resolver
    if _resolver is None:
        settings = get_settings()
        # Created inside a job, so the listener can start on first use.
        _resolver = build_resolver(settings, Database(settings.database), autostart=True)
    return _resolver


def worker_gateway() -> LLMGateway:
    """LLM gateway of the worker process: admin settings, limited parallelism."""
    global _gateway
    if _gateway is None:
        resolver = worker_resolver()
        _gateway = LLMGateway(resolver, concurrency=resolver.concurrency)
    return _gateway


@contextmanager
def use_worker_gateway(gateway: LLMGateway) -> Iterator[None]:
    """Run jobs against ``gateway`` (tests)."""
    global _gateway
    saved, _gateway = _gateway, gateway
    try:
        yield
    finally:
        _gateway = saved

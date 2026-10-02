"""Target types users can export to. A new task system is one ``TodoSink`` subclass plus
one entry in ``FACTORIES``; nothing else in the export changes.

Only types the admin allows (``OLLAMAIL_TODOS_EXPORT_SINKS``) are offered or synced.
"""

from collections.abc import Callable, Mapping
from typing import Any

from app.core.config import TodosSettings, get_settings
from app.todos.export.base import TodoSink
from app.todos.export.caldav import CalDAVSink
from app.todos.export.gtasks import GoogleTasksSink

SinkFactory = Callable[[str, Mapping[str, Any], TodosSettings], TodoSink]


def _caldav(config: Mapping[str, Any], settings: TodosSettings) -> TodoSink:
    return CalDAVSink(
        str(config.get("url", "")),
        str(config.get("username", "")),
        str(config.get("password", "")),
        timeout=settings.export_timeout_seconds,
        allow_http=settings.export_allow_http,
    )


def _gtasks(config: Mapping[str, Any], settings: TodosSettings) -> TodoSink:
    # OAuth client of the Gmail provider (docs/providers/gmail.md §8).
    return GoogleTasksSink(
        str(config.get("refresh_token", "")),
        get_settings().gmail,
        timeout=settings.export_timeout_seconds,
    )


FACTORIES: dict[str, Callable[[Mapping[str, Any], TodosSettings], TodoSink]] = {
    "caldav": _caldav,
    "gtasks": _gtasks,
}


def available_sinks(settings: TodosSettings) -> list[str]:
    """Allowed by the admin and implemented, in the configured order."""
    return [kind for kind in dict.fromkeys(settings.export_sinks) if kind in FACTORIES]


def create_sink(kind: str, config: Mapping[str, Any], settings: TodosSettings) -> TodoSink:
    return FACTORIES[kind](config, settings)

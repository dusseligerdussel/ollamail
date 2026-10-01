"""Structured JSON logging with a privacy filter.

Both structlog loggers and stdlib loggers (uvicorn, SQLAlchemy, libraries) end up in the
same pipeline. The final processors enforce docs/PRIVACY.md:

* ``drop_sensitive_fields`` removes known sensitive keys (mail headers and content,
  prompts, LLM output, credentials) at any nesting depth.
* ``strip_exception_values`` keeps exception types and stack frames but drops exception
  messages and never renders local variables, because both regularly contain mail data.

The filter cannot inspect free text: log messages must be static event names, with
identifiers passed as fields, e.g. ``log.info("message_synced", message_id=...)``.
"""

import logging
import sys
from collections.abc import Mapping
from typing import Any, TextIO

import structlog
from structlog.tracebacks import ExceptionDictTransformer
from structlog.types import EventDict, Processor, WrappedLogger

from app.core.config import LoggingSettings

# Keys whose values must never be logged. A key also matches when it ends with
# ``_<name>`` (``sender_email``, ``reply_to``, ``refresh_token``); matching is
# case-insensitive and treats ``-`` like ``_``.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        # Mail headers and content
        "subject",
        "body",
        "html",
        "text",
        "snippet",
        "content",
        "from",
        "to",
        "cc",
        "bcc",
        "sender",
        "recipient",
        "recipients",
        "email",
        "address",
        "attachment",
        "filename",
        # AI input and output
        "prompt",
        "completion",
        "messages",
        "answer",
        "transcript",
        # Credentials
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "api_key",
    }
)

# Loggers that run their own handlers by default and are rerouted into this pipeline.
_REROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Loggers that put full URLs (paths, query strings) into their messages at INFO/DEBUG.
# URLs can contain personal data (e.g. Graph ``/users/{address}``), so only warnings pass.
_URL_LOGGERS = ("uvicorn.access", "httpx", "httpcore")


def _normalize(key: str) -> str:
    return key.lower().replace("-", "_")


def is_sensitive(key: str) -> bool:
    normalized = _normalize(key)
    if normalized in SENSITIVE_FIELDS:
        return True
    return any(normalized.endswith(f"_{name}") for name in SENSITIVE_FIELDS)


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            k: _scrub(v) for k, v in value.items() if not (isinstance(k, str) and is_sensitive(k))
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub(v) for v in value]
    return value


def drop_sensitive_fields(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Remove sensitive keys from the event, including nested mappings."""
    scrubbed: EventDict = _scrub(event_dict)
    return scrubbed


def strip_exception_values(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Keep exception types and frames, drop exception messages (they may contain PII)."""
    exceptions = event_dict.get("exception")
    if isinstance(exceptions, list):
        for exc in exceptions:
            if isinstance(exc, dict):
                exc.pop("exc_value", None)
                exc.pop("exc_notes", None)
                exc.pop("syntax_error", None)
    elif isinstance(exceptions, str):
        # Pre-rendered traceback text from elsewhere: cannot be sanitised reliably.
        event_dict["exception"] = "[removed]"
    return event_dict


def _drop_color_message(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    # uvicorn duplicates its message with ANSI colors in an extra field.
    event_dict.pop("color_message", None)
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def build_formatter(settings: LoggingSettings) -> structlog.stdlib.ProcessorFormatter:
    renderer: Processor
    if settings.format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            *_shared_processors(),
            structlog.stdlib.ExtraAdder(),
            _drop_color_message,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            strip_exception_values,
            drop_sensitive_fields,
            renderer,
        ],
    )


def configure_logging(settings: LoggingSettings, stream: TextIO | None = None) -> None:
    """Route structlog and stdlib logging through one filtered JSON pipeline."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(build_formatter(settings))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.level)

    for name in _REROUTED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # Request logging is done by RequestContextMiddleware with the route template instead.
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_shared_processors(),
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger

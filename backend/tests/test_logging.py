import io
import json
import logging
from typing import Any

import pytest
import structlog

from app.core.config import LoggingSettings
from app.core.logging import configure_logging, drop_sensitive_fields, get_logger, is_sensitive

# Values that must never show up in log output.
SECRET_VALUES = [
    "Quarterly numbers - confidential",
    "Hello Bob, the password is hunter2",
    "alice@example.com",
    "bob@example.com",
    "Summarize this mail for me",
    "The mail says the deal is off",
    "s3cr3t-refresh-token",
]


@pytest.fixture
def log_output() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG", format="json"), stream=stream)
    return stream


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def _assert_no_secrets(stream: io.StringIO) -> None:
    output = stream.getvalue()
    for value in SECRET_VALUES:
        assert value not in output


@pytest.mark.parametrize(
    "key",
    [
        "subject",
        "Body",
        "from",
        "to",
        "cc",
        "bcc",
        "email",
        "sender_email",
        "reply_to",
        "Reply-To",
        "prompt",
        "completion",
        "refresh_token",
        "password",
        "body_text",
    ],
)
def test_sensitive_keys_are_detected(key: str) -> None:
    assert is_sensitive(key)


@pytest.mark.parametrize(
    "key", ["event", "level", "message_id", "mailbox_id", "status_code", "duration_ms", "model"]
)
def test_neutral_keys_are_kept(key: str) -> None:
    assert not is_sensitive(key)


def test_drop_sensitive_fields_scrubs_nested_values() -> None:
    event = {
        "event": "sync",
        "message_id": "m1",
        "subject": SECRET_VALUES[0],
        "meta": {"to": [SECRET_VALUES[2]], "folder_id": "f1"},
        "items": [{"email": SECRET_VALUES[3], "uid": 7}],
    }

    result = drop_sensitive_fields(None, "info", event)

    assert result == {
        "event": "sync",
        "message_id": "m1",
        "meta": {"folder_id": "f1"},
        "items": [{"uid": 7}],
    }


def test_structlog_events_do_not_contain_pii(log_output: io.StringIO) -> None:
    get_logger("test").info(
        "message_processed",
        message_id="m-42",
        subject=SECRET_VALUES[0],
        body=SECRET_VALUES[1],
        sender_email=SECRET_VALUES[2],
        to=[SECRET_VALUES[3]],
        prompt=SECRET_VALUES[4],
        completion=SECRET_VALUES[5],
        llm={"model": "small", "prompt": SECRET_VALUES[4]},
        refresh_token=SECRET_VALUES[6],
    )

    _assert_no_secrets(log_output)
    [record] = _records(log_output)
    assert record["event"] == "message_processed"
    assert record["message_id"] == "m-42"
    assert record["llm"] == {"model": "small"}
    assert record["level"] == "info"
    assert "timestamp" in record


def test_stdlib_extra_fields_do_not_contain_pii(log_output: io.StringIO) -> None:
    logging.getLogger("some.library").warning(
        "library_event", extra={"subject": SECRET_VALUES[0], "mailbox_id": "mb-1"}
    )

    _assert_no_secrets(log_output)
    [record] = _records(log_output)
    assert record["event"] == "library_event"
    assert record["mailbox_id"] == "mb-1"


def test_exception_messages_and_locals_are_not_logged(log_output: io.StringIO) -> None:
    def deliver(recipient_list: list[str]) -> None:
        raise ValueError(f"mailbox {recipient_list[0]} rejected")

    try:
        deliver([SECRET_VALUES[2]])
    except ValueError:
        get_logger("test").exception("delivery_failed")

    _assert_no_secrets(log_output)
    [record] = _records(log_output)
    [exc] = record["exception"]
    assert exc["exc_type"] == "ValueError"
    assert exc["frames"]


def test_stdlib_exceptions_are_sanitised(log_output: io.StringIO) -> None:
    try:
        raise RuntimeError(SECRET_VALUES[1])
    except RuntimeError:
        logging.getLogger("some.library").exception("library_failed")

    _assert_no_secrets(log_output)
    [record] = _records(log_output)
    assert record["exception"][0]["exc_type"] == "RuntimeError"


def test_console_format_renders_sanitised_exceptions() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="DEBUG", format="console"), stream=stream)
    try:
        raise RuntimeError(SECRET_VALUES[1])
    except RuntimeError:
        logging.getLogger("some.library").exception("library_failed")
        get_logger("test").exception("delivery_failed")

    output = stream.getvalue()
    _assert_no_secrets(stream)
    # Without the text conversion the formatter fails and both records are lost.
    assert "library_failed" in output
    assert "delivery_failed" in output
    assert output.count("RuntimeError") == 2


def test_context_variables_are_filtered(log_output: io.StringIO) -> None:
    structlog.contextvars.bind_contextvars(request_id="r-1", email=SECRET_VALUES[2])
    try:
        get_logger("test").info("with_context")
    finally:
        structlog.contextvars.clear_contextvars()

    _assert_no_secrets(log_output)
    assert _records(log_output)[0]["request_id"] == "r-1"


@pytest.mark.parametrize("logger", ["uvicorn.access", "httpx", "httpcore"])
def test_url_logging_is_suppressed(log_output: io.StringIO, logger: str) -> None:
    logging.getLogger(logger).info('"GET /api/search?q=%s HTTP/1.1" 200', "alice")

    assert log_output.getvalue() == ""


def test_level_filters_events() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(level="WARNING"), stream=stream)

    get_logger("test").info("not_shown")
    get_logger("test").warning("shown")

    assert [r["event"] for r in _records(stream)] == ["shown"]

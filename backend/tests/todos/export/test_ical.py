"""VTODO writing and reading (RFC 5545 escaping, folding, status mapping)."""

from datetime import UTC, date, datetime

import pytest

from app.todos.export.base import TaskData
from app.todos.export.ical import (
    ICalError,
    build_vtodo,
    escape_text,
    fold,
    parse_vtodo,
    unescape_text,
    unfold,
)
from app.todos.models import TodoPriority, TodoStatus

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def data(**changes: object) -> TaskData:
    values: dict[str, object] = {
        "uid": "0192f0a0-0000-7000-8000-000000000001",
        "title": "Angebot prüfen; Preise, Fristen",
        "description": "Zeile 1\nZeile 2 \\ Ende",
        "due_date": date(2026, 10, 9),
        "priority": TodoPriority.LOW,
        "status": TodoStatus.OPEN,
        "completed_at": None,
        "modified_at": datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
        "url": None,
    }
    values.update(changes)
    return TaskData(**values)  # type: ignore[arg-type]


def test_text_escaping_round_trips() -> None:
    text = "a;b,c\\d\ne\r\nf"
    assert escape_text(text) == "a\\;b\\,c\\\\d\\ne\\nf"
    assert unescape_text(escape_text(text)) == "a;b,c\\d\ne\nf"


def test_long_lines_are_folded_without_splitting_characters() -> None:
    line = "SUMMARY:" + "ä" * 80
    folded = fold(line)

    parts = folded.split("\r\n")
    assert all(len(part.encode()) <= 75 for part in parts)
    assert all(part.startswith(" ") for part in parts[1:])
    assert unfold(folded) == [line]


def test_build_vtodo() -> None:
    body = build_vtodo(data(), now=NOW)
    lines = unfold(body)

    assert body.endswith("\r\n")
    assert lines[:4] == ["BEGIN:VCALENDAR", "VERSION:2.0", lines[2], "BEGIN:VTODO"]
    assert "UID:0192f0a0-0000-7000-8000-000000000001" in lines
    assert "DTSTAMP:20261007T100000Z" in lines
    assert "LAST-MODIFIED:20261007T090000Z" in lines
    assert "SUMMARY:Angebot prüfen\\; Preise\\, Fristen" in lines
    assert "DESCRIPTION:Zeile 1\\nZeile 2 \\\\ Ende" in lines
    assert "DUE;VALUE=DATE:20261009" in lines
    assert "PRIORITY:9" in lines
    assert "STATUS:NEEDS-ACTION" in lines
    assert not any(line.startswith(("COMPLETED", "URL")) for line in lines)


def test_done_and_dismissed() -> None:
    done = unfold(build_vtodo(data(status=TodoStatus.DONE, completed_at=NOW), now=NOW))
    dismissed = unfold(build_vtodo(data(status=TodoStatus.DISMISSED), now=NOW))

    assert {"STATUS:COMPLETED", "COMPLETED:20261007T100000Z", "PERCENT-COMPLETE:100"} <= set(done)
    assert "STATUS:CANCELLED" in dismissed


def test_mail_link_goes_into_url_and_description() -> None:
    url = "https://mail.example.org/inbox?message=0192"
    lines = unfold(build_vtodo(data(description=None, url=url), now=NOW))

    assert f"URL:{url}" in lines
    assert f"DESCRIPTION:{url}" in lines


@pytest.mark.parametrize(
    ("status_line", "expected"),
    [
        ("STATUS:NEEDS-ACTION", TodoStatus.OPEN),
        ("STATUS:IN-PROCESS", TodoStatus.OPEN),
        ("STATUS:completed", TodoStatus.DONE),
        ("STATUS:CANCELLED", TodoStatus.DISMISSED),
        ("COMPLETED:20261008T120000Z", TodoStatus.DONE),
        ("", TodoStatus.OPEN),
    ],
)
def test_parse_status(status_line: str, expected: TodoStatus) -> None:
    body = (
        "BEGIN:VCALENDAR\r\nBEGIN:VTODO\r\nUID:x\r\n"
        f"{status_line}\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    )
    assert parse_vtodo(body).status == expected


def test_parse_ignores_nested_components_and_reads_folded_lines() -> None:
    body = (
        "BEGIN:VCALENDAR\nBEGIN:VTODO\nUID:abc-\n 123\nLAST-MODIFIED;X-P=1:20261008T120000Z\n"
        "BEGIN:VALARM\nSTATUS:COMPLETED\nEND:VALARM\nSTATUS:NEEDS-ACTION\nEND:VTODO\n"
        "END:VCALENDAR\n"
    )
    parsed = parse_vtodo(body)

    assert parsed.uid == "abc-123"
    assert parsed.status == TodoStatus.OPEN
    assert parsed.modified_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_round_trip() -> None:
    parsed = parse_vtodo(build_vtodo(data(status=TodoStatus.DONE, completed_at=NOW), now=NOW))

    assert parsed.uid == "0192f0a0-0000-7000-8000-000000000001"
    assert parsed.status == TodoStatus.DONE
    assert parsed.modified_at == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def test_no_vtodo_is_an_error() -> None:
    with pytest.raises(ICalError):
        parse_vtodo("BEGIN:VCALENDAR\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR\n")

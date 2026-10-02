"""Minimal iCalendar (RFC 5545) support for VTODO: write one task, read back its status.

Only what the export needs: text escaping, line folding at 75 octets, unfolding, and the
properties ``UID``, ``STATUS``, ``LAST-MODIFIED`` and ``COMPLETED``. Other components
and properties are ignored.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.todos.export.base import TaskData
from app.todos.models import TodoPriority, TodoStatus

PRODID = "-//ollamail//Todo export//EN"
# RFC 5545 PRIORITY: 1 highest, 9 lowest, 0 undefined.
_PRIORITY = {TodoPriority.HIGH: 1, TodoPriority.NORMAL: 5, TodoPriority.LOW: 9}
_STATUS = {
    TodoStatus.OPEN: "NEEDS-ACTION",
    TodoStatus.DONE: "COMPLETED",
    TodoStatus.DISMISSED: "CANCELLED",
}
_FROM_STATUS = {
    "NEEDS-ACTION": TodoStatus.OPEN,
    "IN-PROCESS": TodoStatus.OPEN,
    "COMPLETED": TodoStatus.DONE,
    "CANCELLED": TodoStatus.DISMISSED,
}
MAX_LINE_OCTETS = 75


class ICalError(ValueError):
    """Not a calendar object with exactly one VTODO."""


def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Split a content line into lines of at most 75 octets, never inside a character."""
    parts: list[str] = []
    current = ""
    size = 0
    limit = MAX_LINE_OCTETS
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            parts.append(current)
            current, size = "", 0
            # Continuation lines start with a space, which counts.
            limit = MAX_LINE_OCTETS - 1
        current += char
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _date(value: date) -> str:
    return value.strftime("%Y%m%d")


def build_vtodo(task: TaskData, *, now: datetime) -> str:
    """One VCALENDAR with one VTODO; the mail link goes into URL and the description."""
    description = task.description or ""
    if task.url:
        description = f"{description}\n\n{task.url}" if description else task.url
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VTODO",
        f"UID:{escape_text(task.uid)}",
        f"DTSTAMP:{_utc(now)}",
        f"LAST-MODIFIED:{_utc(task.modified_at)}",
        f"SUMMARY:{escape_text(task.title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{escape_text(description)}")
    if task.url:
        # URI value: not text-escaped, but it must not break the line structure.
        lines.append(f"URL:{task.url.replace(chr(13), '').replace(chr(10), '')}")
    if task.due_date is not None:
        lines.append(f"DUE;VALUE=DATE:{_date(task.due_date)}")
    lines.append(f"PRIORITY:{_PRIORITY[task.priority]}")
    lines.append(f"STATUS:{_STATUS[task.status]}")
    if task.status == TodoStatus.DONE:
        lines.append(f"COMPLETED:{_utc(task.completed_at or task.modified_at)}")
        lines.append("PERCENT-COMPLETE:100")
    lines += ["END:VTODO", "END:VCALENDAR"]
    return "".join(f"{fold(line)}\r\n" for line in lines)


@dataclass(frozen=True)
class ParsedTodo:
    uid: str | None
    status: TodoStatus
    modified_at: datetime | None


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in {" ", "\t"} and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, str]:
    """Property name (upper case, without parameters) and raw value."""
    head, sep, value = line.partition(":")
    if not sep:
        return "", ""
    return head.split(";", 1)[0].strip().upper(), value


def unescape_text(value: str) -> str:
    result = []
    chars = iter(value)
    for char in chars:
        if char == "\\":
            nxt = next(chars, "")
            result.append("\n" if nxt in {"n", "N"} else nxt)
        else:
            result.append(char)
    return "".join(result)


def parse_datetime(value: str) -> datetime | None:
    """``20261009T120000Z`` (UTC) or a floating time (treated as UTC); else ``None``."""
    value = value.strip()
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_vtodo(text: str) -> ParsedTodo:
    """Status, UID and modification time of the first VTODO in a calendar object."""
    props: dict[str, str] = {}
    depth = 0
    found = False
    for line in unfold(text):
        name, value = _split(line)
        if name == "BEGIN":
            depth += 1
            if value.strip().upper() == "VTODO" and not found:
                found = True
                props = {"__depth": str(depth)}
            continue
        if name == "END":
            if found and value.strip().upper() == "VTODO" and "__end" not in props:
                props["__end"] = "1"
            depth -= 1
            continue
        # Only direct properties of the VTODO (not of a nested VALARM).
        if found and "__end" not in props and str(depth) == props["__depth"]:
            props.setdefault(name, value)
    if not found:
        raise ICalError("no VTODO")
    status_raw = props.get("STATUS", "NEEDS-ACTION").strip().upper()
    status = _FROM_STATUS.get(status_raw, TodoStatus.OPEN)
    if "STATUS" not in props and "COMPLETED" in props:
        status = TodoStatus.DONE
    modified = parse_datetime(props.get("LAST-MODIFIED", "")) or parse_datetime(
        props.get("DTSTAMP", "")
    )
    uid = unescape_text(props["UID"]).strip() if "UID" in props else None
    return ParsedTodo(uid=uid, status=status, modified_at=modified)

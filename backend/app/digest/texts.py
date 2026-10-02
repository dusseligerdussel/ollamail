"""Fixed parts of a digest script (title, counts, newsletters, todos) in German and English.

They are written by code, not by the model: counts, dates and todo titles must be exact,
and small models are least reliable at exactly these. Numbers and dates are left as
digits; the TTS normalisation speaks them (``app.ai.tts.normalize``).
"""

from collections.abc import Sequence
from datetime import date
from typing import Literal

from app.digest.content import DigestContent, TodoItem

DigestLanguage = Literal["de", "en"]

WEEKDAYS: dict[DigestLanguage, tuple[str, ...]] = {
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
MONTHS: dict[DigestLanguage, tuple[str, ...]] = {
    "de": (
        "Januar",
        "Februar",
        "März",
        "April",
        "Mai",
        "Juni",
        "Juli",
        "August",
        "September",
        "Oktober",
        "November",
        "Dezember",
    ),
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
}
# Senders named in the collective sentence of a bulk category.
MAX_NAMED_SENDERS = 3


def language_of(value: str | None) -> DigestLanguage:
    return "de" if (value or "").lower().startswith("de") else "en"


def long_date(day: date, lang: DigestLanguage) -> str:
    weekday = WEEKDAYS[lang][day.weekday()]
    month = MONTHS[lang][day.month - 1]
    if lang == "de":
        return f"{weekday}, {day.day}. {month} {day.year}"
    return f"{weekday}, {month} {day.day}, {day.year}"


def title(day: date, lang: DigestLanguage) -> str:
    prefix = "Digest vom" if lang == "de" else "Digest for"
    return f"{prefix} {long_date(day, lang)}"


def _join(items: Sequence[str], lang: DigestLanguage) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    last = " und " if lang == "de" else " and "
    return ", ".join(items[:-1]) + last + items[-1]


def intro(day: date, content: DigestContent, lang: DigestLanguage) -> str:
    count = content.message_count
    important = sum(1 for mail in content.mails if mail.is_important)
    if lang == "de":
        weekday, month = WEEKDAYS["de"][day.weekday()], MONTHS["de"][day.month - 1]
        # "am Freitag, dem 2. Oktober": the dative makes the TTS say "zweiten".
        text = f"Dein Überblick am {weekday}, dem {day.day}. {month} {day.year}."
        if count == 0:
            return text + " Seit dem letzten Digest sind keine neuen Mails eingegangen."
        if count == 1:
            text += " Seit dem letzten Digest ist eine neue Mail eingegangen"
            return text + (", sie ist wichtig." if important else ".")
        text += f" Seit dem letzten Digest sind {count} neue Mails eingegangen"
        if important == 1:
            text += ", eine davon ist wichtig"
        elif important:
            text += f", {important} davon sind wichtig"
        return text + "."
    text = f"Your overview for {long_date(day, lang)}."
    if count == 0:
        return text + " No new mail has arrived since the last digest."
    if count == 1:
        text += " Since the last digest, one new mail has arrived"
        return text + (", and it is important." if important else ".")
    text += f" Since the last digest, {count} new mails have arrived"
    if important:
        text += f", {important} of them important"
    return text + "."


def more_mails(count: int, lang: DigestLanguage) -> str:
    if lang == "de":
        if count == 1:
            return "Dazu kommt eine weitere Mail, die hier nicht einzeln vorkommt."
        return f"Dazu kommen {count} weitere Mails, die hier nicht einzeln vorkommen."
    if count == 1:
        return "There is one more mail that is not covered here."
    return f"There are {count} more mails that are not covered here."


_BULK_NAMES: dict[DigestLanguage, dict[str, tuple[str, str]]] = {
    "de": {
        "newsletter": ("ein Newsletter", "{n} Newsletter"),
        "notification": (
            "eine automatische Benachrichtigung",
            "{n} automatische Benachrichtigungen",
        ),
    },
    "en": {
        "newsletter": ("one newsletter", "{n} newsletters"),
        "notification": ("one automated notification", "{n} automated notifications"),
    },
}


def _bulk_part(key: str, senders: Sequence[str], lang: DigestLanguage) -> str:
    one, many = _BULK_NAMES[lang].get(
        key,
        ("eine weitere Mail", "{n} weitere Mails")
        if lang == "de"
        else ("one more mail", "{n} more mails"),
    )
    count = len(senders)
    text = one if count == 1 else many.format(n=count)
    named = list(dict.fromkeys(s for s in senders if s))[:MAX_NAMED_SENDERS]
    if named:
        if lang == "de":
            prefix = " von " if count == 1 else " unter anderem von "
        else:
            prefix = " from " if count == 1 else ", among others from "
        text += prefix + _join(named, lang)
    return text


def bulk(groups: dict[str, list[str]], lang: DigestLanguage) -> str | None:
    """One collective sentence for newsletters, notifications and other bulk mail."""
    parts = [_bulk_part(key, senders, lang) for key, senders in groups.items() if senders]
    if not parts:
        return None
    if lang == "de":
        verb = "kam" if sum(len(senders) for senders in groups.values()) == 1 else "kamen"
        return f"Außerdem {verb} {_join(parts, lang)}."
    return f"Also: {_join(parts, lang)}."


def _todo_titles(todos: Sequence[TodoItem]) -> str:
    return "; ".join(todo.title.rstrip(".!") for todo in todos)


def todos(content: DigestContent, today: date, lang: DigestLanguage) -> list[str]:
    """Sentences about new todos and todos that are overdue or due today or tomorrow."""
    labels = {
        "de": ("Neue Aufgaben", "Überfällig", "Heute fällig", "Morgen fällig"),
        "en": ("New todos", "Overdue", "Due today", "Due tomorrow"),
    }[lang]
    groups: list[tuple[str, list[TodoItem]]] = [(labels[0], list(content.new_todos))]
    overdue = [t for t in content.due_todos if t.due_date is not None and t.due_date < today]
    due_today = [t for t in content.due_todos if t.due_date == today]
    due_tomorrow = [t for t in content.due_todos if t.due_date is not None and t.due_date > today]
    groups += [(labels[1], overdue), (labels[2], due_today), (labels[3], due_tomorrow)]
    return [f"{label}: {_todo_titles(items)}." for label, items in groups if items]


def closing(lang: DigestLanguage) -> str:
    return "Das war dein Digest." if lang == "de" else "That's your digest."


def fallback_sentence(sender: str, subject: str, lang: DigestLanguage) -> str:
    """Stands in for a mail the model did not summarise."""
    subject = subject.strip() or ("ohne Betreff" if lang == "de" else "no subject")
    if lang == "de":
        return f"{sender or 'Unbekannt'} schreibt: {subject}."
    return f"{sender or 'Unknown sender'} wrote: {subject}."

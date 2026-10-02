"""Resolve due-date phrases ("bis nächsten Freitag", "by end of month") to dates.

Small models are unreliable at date arithmetic, so the model returns the phrase from
the mail plus its own guess, and this module computes the date deterministically. The
reference is the day the mail was sent, in the user's time zone. The model's guess is
only used if the phrase is not understood.

Ambiguous phrases resolve to the *earlier* date, which is the safer one for a deadline:
"nächsten Freitag" / "next Friday" is the next Friday after the reference day, "Freitag
nächster Woche" / "Friday next week" the Friday of the following calendar week. A bare
weekday ("bis Freitag") may be the reference day itself.
"""

import calendar
import re
from datetime import date, timedelta

WEEKDAYS = {
    "montag": 0,
    "monday": 0,
    "dienstag": 1,
    "tuesday": 1,
    "mittwoch": 2,
    "wednesday": 2,
    "donnerstag": 3,
    "thursday": 3,
    "freitag": 4,
    "friday": 4,
    "samstag": 5,
    "sonnabend": 5,
    "saturday": 5,
    "sonntag": 6,
    "sunday": 6,
}
MONTHS = {
    "januar": 1,
    "jänner": 1,
    "january": 1,
    "jan": 1,
    "februar": 2,
    "february": 2,
    "feb": 2,
    "märz": 3,
    "maerz": 3,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "mai": 5,
    "may": 5,
    "juni": 6,
    "june": 6,
    "jun": 6,
    "juli": 7,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "oktober": 10,
    "october": 10,
    "okt": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "dezember": 12,
    "december": 12,
    "dez": 12,
    "dec": 12,
}
NUMBERS = {
    "ein": 1,
    "eine": 1,
    "einem": 1,
    "einer": 1,
    "a": 1,
    "an": 1,
    "one": 1,
    "zwei": 2,
    "two": 2,
    "drei": 3,
    "three": 3,
    "vier": 4,
    "four": 4,
    "fünf": 5,
    "five": 5,
    "sechs": 6,
    "six": 6,
    "sieben": 7,
    "seven": 7,
    "acht": 8,
    "eight": 8,
    "neun": 9,
    "nine": 9,
    "zehn": 10,
    "ten": 10,
    "vierzehn": 14,
    "fourteen": 14,
}
# Model guesses further away than this are discarded as hallucinated.
MAX_HORIZON = timedelta(days=2 * 366)

_WEEKDAY = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MONTH = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUMBER = r"\d{1,3}|" + "|".join(sorted(NUMBERS, key=len, reverse=True))

_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DOTTED = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}|\d{2})\b)?")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})\.?\s+(?:of\s+)?({_MONTH})\.?(?:\s+(\d{{4}}))?\b")
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?")
_IN_PERIOD = re.compile(rf"\bin\s+({_NUMBER})\s+(tag|tagen|day|days|woche|wochen|week|weeks)\b")
_WEEKDAY_NEXT_WEEK = re.compile(
    rf"\b({_WEEKDAY})\b,?\s+(?:(?:in\s+)?der\s+)?(?:nächste[nr]?|kommende[nr]?|next)\s+(?:woche|week)\b"
)
_AFTER_NEXT_WEEKDAY = re.compile(rf"\bübernächste[nr]?\s+({_WEEKDAY})\b")
_NEXT_WEEKDAY = re.compile(
    rf"\b(?:nächste[nr]?|kommende[nr]?|next|(?:this\s+)?coming)\s+({_WEEKDAY})\b"
)
_WEEKDAY_ANY = re.compile(rf"\b({_WEEKDAY})\b")
_END_OF_MONTH = re.compile(
    r"\b(?:ende\s+(?:des|dieses)\s+monats|monatsende|end\s+of\s+(?:the\s+)?month)\b"
)
_END_OF_NEXT_WEEK = re.compile(
    r"\b(?:ende\s+(?:der\s+)?(?:nächste[nr]?|kommende[nr]?)\s+woche|end\s+of\s+next\s+week)\b"
)
_NEXT_WEEK = re.compile(
    r"\b(?:(?:anfang\s+)?(?:nächste[nr]?|kommende[nr]?)\s+woche|(?:early\s+)?next\s+week)\b"
)
_END_OF_WEEK = re.compile(
    r"\b(?:ende\s+(?:der|dieser)\s+woche|wochenende|end\s+of\s+(?:the|this)\s+week|"
    r"diese\s+woche|this\s+week)\b"
)
_DAY_AFTER_TOMORROW = re.compile(r"\b(?:übermorgen|day\s+after\s+tomorrow)\b")
_TOMORROW = re.compile(r"\b(?:morgen|tomorrow)\b")
_TODAY = re.compile(r"\b(?:heute|today|tonight|end\s+of\s+(?:the\s+)?day|eod)\b")


def _number(token: str) -> int:
    return int(token) if token.isdigit() else NUMBERS[token]


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _without_year(month: int, day: int, reference: date) -> date | None:
    """Day and month without a year: the next such date on or after ``reference``."""
    candidate = _date(reference.year, month, day)
    if candidate is not None and candidate < reference:
        candidate = _date(reference.year + 1, month, day)
    return candidate


def _year(token: str) -> int:
    value = int(token)
    return value + 2000 if value < 100 else value


def _on_or_after(reference: date, weekday: int) -> date:
    return reference + timedelta(days=(weekday - reference.weekday()) % 7)


def _in_next_week(reference: date, weekday: int) -> date:
    monday = reference - timedelta(days=reference.weekday()) + timedelta(days=7)
    return monday + timedelta(days=weekday)


def _explicit(text: str, reference: date) -> date | None:
    if match := _ISO.search(text):
        return _date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DOTTED.search(text):
        day, month = int(match[1]), int(match[2])
        if match[3]:
            return _date(_year(match[3]), month, day)
        return _without_year(month, day, reference)
    if match := _DAY_MONTH.search(text):
        day, month = int(match[1]), MONTHS[match[2]]
        return (
            _date(int(match[3]), month, day) if match[3] else _without_year(month, day, reference)
        )
    if match := _MONTH_DAY.search(text):
        month, day = MONTHS[match[1]], int(match[2])
        return (
            _date(int(match[3]), month, day) if match[3] else _without_year(month, day, reference)
        )
    return None


def _relative(text: str, reference: date) -> date | None:
    if match := _IN_PERIOD.search(text):
        amount = _number(match[1])
        days = amount * 7 if match[2].startswith(("woche", "week")) else amount
        return reference + timedelta(days=days)
    if match := _WEEKDAY_NEXT_WEEK.search(text):
        return _in_next_week(reference, WEEKDAYS[match[1]])
    if match := _AFTER_NEXT_WEEKDAY.search(text):
        return _on_or_after(reference + timedelta(days=1), WEEKDAYS[match[1]]) + timedelta(days=7)
    if match := _NEXT_WEEKDAY.search(text):
        return _on_or_after(reference + timedelta(days=1), WEEKDAYS[match[1]])
    if match := _WEEKDAY_ANY.search(text):
        return _on_or_after(reference, WEEKDAYS[match[1]])
    if _END_OF_MONTH.search(text):
        last = calendar.monthrange(reference.year, reference.month)[1]
        return reference.replace(day=last)
    if _END_OF_NEXT_WEEK.search(text):
        return _in_next_week(reference, 4)
    if _NEXT_WEEK.search(text):
        return _in_next_week(reference, 0)
    if _END_OF_WEEK.search(text):
        return max(reference, reference + timedelta(days=4 - reference.weekday()))
    if _DAY_AFTER_TOMORROW.search(text):
        return reference + timedelta(days=2)
    if _TOMORROW.search(text):
        return reference + timedelta(days=1)
    if _TODAY.search(text):
        return reference
    return None


def parse_due_phrase(phrase: str, reference: date) -> date | None:
    """Date for a phrase such as "bis 15.10.", "next Friday", "in zwei Wochen"."""
    text = " ".join(phrase.lower().split())
    return _explicit(text, reference) or _relative(text, reference)


def _plausible(value: date, reference: date) -> bool:
    return reference <= value <= reference + MAX_HORIZON


def resolve_due_date(phrase: str | None, guess: str | None, reference: date) -> date | None:
    """Due date from the phrase in the mail, else from the model's ISO date guess.

    ``reference`` is the day the mail was sent in the user's time zone. Model guesses in
    the past or implausibly far in the future are dropped.
    """
    if phrase:
        parsed = parse_due_phrase(phrase, reference)
        if parsed is not None:
            return parsed
    if guess:
        try:
            value = date.fromisoformat(guess.strip()[:10])
        except ValueError:
            return None
        if _plausible(value, reference):
            return value
    return None

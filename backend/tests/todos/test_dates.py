"""Unit tests: due-date phrases resolved against the day a mail was sent."""

from datetime import date

import pytest

from app.todos.dates import parse_due_phrase, resolve_due_date

# A Wednesday.
WED = date(2026, 10, 7)
FRI = date(2026, 10, 9)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("bis nächsten Freitag", date(2026, 10, 9)),
        ("by next Friday", date(2026, 10, 9)),
        ("kommenden Montag", date(2026, 10, 12)),
        ("Freitag nächster Woche", date(2026, 10, 16)),
        ("Friday next week", date(2026, 10, 16)),
        ("übernächsten Freitag", date(2026, 10, 16)),
        ("bis Freitag", date(2026, 10, 9)),
        ("am Mittwoch", date(2026, 10, 7)),
        ("by Tuesday", date(2026, 10, 13)),
        ("heute", WED),
        ("end of day", WED),
        ("bis morgen", date(2026, 10, 8)),
        ("tomorrow", date(2026, 10, 8)),
        ("übermorgen", date(2026, 10, 9)),
        ("in 3 Tagen", date(2026, 10, 10)),
        ("in zwei Wochen", date(2026, 10, 21)),
        ("in a week", date(2026, 10, 14)),
        ("Ende der Woche", date(2026, 10, 9)),
        ("by the end of this week", date(2026, 10, 9)),
        ("nächste Woche", date(2026, 10, 12)),
        ("Ende nächster Woche", date(2026, 10, 16)),
        ("end of next week", date(2026, 10, 16)),
        ("bis Monatsende", date(2026, 10, 31)),
        ("by the end of the month", date(2026, 10, 31)),
        ("am 15.10.", date(2026, 10, 15)),
        ("bis 3.1.", date(2027, 1, 3)),
        ("bis 15.10.2026", date(2026, 10, 15)),
        ("bis 15.10.26", date(2026, 10, 15)),
        ("2026-11-02", date(2026, 11, 2)),
        ("bis zum 15. Oktober", date(2026, 10, 15)),
        ("by October 20th", date(2026, 10, 20)),
        ("by 5 November 2026", date(2026, 11, 5)),
        ("bis Montag, 19.10.", date(2026, 10, 19)),
    ],
)
def test_phrases(phrase: str, expected: date) -> None:
    assert parse_due_phrase(phrase, WED) == expected


def test_bare_weekday_on_that_day_is_the_same_day() -> None:
    assert parse_due_phrase("bis Freitag", FRI) == FRI
    assert parse_due_phrase("nächsten Freitag", FRI) == date(2026, 10, 16)


def test_end_of_week_on_the_weekend_is_the_reference_day() -> None:
    saturday = date(2026, 10, 10)
    assert parse_due_phrase("Ende der Woche", saturday) == saturday


@pytest.mark.parametrize("phrase", ["asap", "bald", "when you find the time", "31.02.", ""])
def test_unknown_or_invalid_phrases(phrase: str) -> None:
    assert parse_due_phrase(phrase, WED) is None


def test_phrase_wins_over_the_model_guess() -> None:
    # Small models get weekday arithmetic wrong; the phrase is computed instead.
    assert resolve_due_date("bis nächsten Freitag", "2026-10-10", WED) == FRI


def test_model_guess_is_used_if_the_phrase_is_unknown() -> None:
    assert resolve_due_date("Anfang Q4", "2026-10-20", WED) == date(2026, 10, 20)
    assert resolve_due_date(None, "2026-10-20T00:00:00", WED) == date(2026, 10, 20)


@pytest.mark.parametrize("guess", ["2026-10-06", "2030-01-01", "next week", "", None])
def test_implausible_guesses_are_dropped(guess: str | None) -> None:
    assert resolve_due_date(None, guess, WED) is None

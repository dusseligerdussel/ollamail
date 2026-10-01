import pytest

from app.ai.tts.normalize import normalize


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        # Dates
        ("on October 3, 2026", "on October third, twenty twenty-six"),
        ("by Oct. 3rd", "by October third"),
        ("on 3 October 2026", "on the third of October twenty twenty-six"),
        ("on the 21st", "on the twenty-first"),
        ("due 2026-10-01", "due October first, twenty twenty-six"),
        # Times
        ("at 2:30 pm", "at two thirty p m"),
        ("at 9 a.m.", "at nine a m"),
        ("at 9:05 AM", "at nine oh five a m"),
        ("at 14:00", "at two p m"),
        ("at 00:30", "at twelve thirty a m"),
        ("at 12:15", "at twelve fifteen p m"),
        # Amounts and percentages
        ("$1,234.50", "one thousand two hundred and thirty-four dollars and fifty cents"),
        ("$1", "one dollar"),
        ("€2.05", "two euros and five cents"),
        ("5 EUR", "five euros"),
        ("15% off", "fifteen percent off"),
        ("2.5 percent", "two point five percent"),
        # Numbers
        ("3.5 hours", "three point five hours"),
        ("1,000,000 users", "one million users"),
        ("in 1999", "in nineteen ninety-nine"),
        ("12 new emails", "twelve new emails"),
        ("pages 3-5", "pages three to five"),
        ("room 007", "room zero zero seven"),
        # Abbreviations
        ("e.g. today", "for example today"),
        ("i. e. tomorrow", "that is tomorrow"),
        ("Dr. Smith and Mr. Jones", "Doctor Smith and Mister Jones"),
        ("approx. 3 hours", "approximately three hours"),
        ("see p. 12, No. 5", "see page twelve, number five"),
        ("cats vs. dogs", "cats versus dogs"),
        # Symbols, links and markup
        ("Smith & Jones", "Smith and Jones"),
        ("visit www.example.com now", "visit link now"),
        ("write to bob@example.co.uk", "write to bob at example dot co dot uk"),
        ("__Note:__ read this", "Note: read this"),
        ("-3 °C", "minus three degrees Celsius"),
    ],
)
def test_normalize_english(text: str, spoken: str) -> None:
    assert normalize(text, "en") == spoken


def test_full_sentence() -> None:
    text = "The meeting is on October 3, 2026 at 2:30 pm, e.g. in room 2."

    assert normalize(text, "en") == (
        "The meeting is on October third, twenty twenty-six at two thirty p m, "
        "for example in room two."
    )


def test_language_specific_separators() -> None:
    # "1.234" is a thousand in German, a decimal in English.
    assert normalize("1.234", "de") == "eintausendzweihundertvierunddreißig"
    assert normalize("1.234", "en") == "one point two three four"

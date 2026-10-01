import pytest

from app.ai.tts.normalize import normalize


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        # Dates: dative after prepositions, nominative otherwise
        ("am 3.10.2026", "am dritten Oktober zweitausendsechsundzwanzig"),
        ("bis 15.10. fällig", "bis fünfzehnten Oktober fällig"),
        ("Frist: 1.12.", "Frist: erster Dezember"),
        ("vom 03.10.26", "vom dritten Oktober zweitausendsechsundzwanzig"),
        ("am 2026-10-01", "am ersten Oktober zweitausendsechsundzwanzig"),
        ("am 3. Oktober 2026", "am dritten Oktober zweitausendsechsundzwanzig"),
        ("seit 1. Jan.", "seit ersten Januar"),
        # Not a date: left as numbers
        ("am 32.13.2026", "am zweiunddreißig.dreizehn.zweitausendsechsundzwanzig"),
        # Times
        ("um 14:30 Uhr", "um vierzehn Uhr dreißig"),
        ("um 9.05 Uhr", "um neun Uhr fünf"),
        ("um 1 Uhr", "um ein Uhr"),
        ("ab 08:00", "ab acht Uhr"),
        ("9\u201317 Uhr", "neun bis siebzehn Uhr"),
        # Amounts and percentages
        ("1.234,56 €", "eintausendzweihundertvierunddreißig Euro sechsundfünfzig"),
        ("12,- €", "zwölf Euro"),
        ("1 €", "ein Euro"),
        ("EUR 5,99", "fünf Euro neunundneunzig"),
        ("15 % Rabatt", "fünfzehn Prozent Rabatt"),
        ("1 Prozent", "ein Prozent"),
        ("2,5 %", "zwei Komma fünf Prozent"),
        # Numbers
        ("3,5 Mio. Nutzer", "drei Komma fünf Millionen Nutzer"),
        ("1.000.000 Mails", "eine Million Mails"),
        ("im Jahr 1987", "im Jahr neunzehnhundertsiebenundachtzig"),
        ("3 neue Mails", "drei neue Mails"),
        ("Raum 07", "Raum null sieben"),
        ("-5 °C", "minus fünf Grad Celsius"),
        ("Seiten 3-5", "Seiten drei bis fünf"),
        ("Angebot Nr. 2026-17", "Angebot Nummer zweitausendsechsundzwanzig-siebzehn"),
        (
            "Tel. 0171 1234567",
            "Telefon null eins sieben eins eins zwei drei vier fünf sechs sieben",
        ),
        ("+49 30 123456", "plus vier neun drei null eins zwei drei vier fünf sechs"),
        # Abbreviations
        ("z. B. heute", "zum Beispiel heute"),
        ("z.B. heute", "zum Beispiel heute"),
        ("d. h. morgen", "das heißt morgen"),
        ("usw.", "und so weiter"),
        ("ca. 20 Min.", "circa zwanzig Minuten"),
        ("Hr. Weber und Fr. Meier", "Herr Weber und Frau Meier"),
        ("Fr., 3.10.", "Freitag, dritter Oktober"),
        ("Mo.-Fr.", "Montag-Freitag"),
        ("Dr. Schmidt", "Doktor Schmidt"),
        ("KW 40", "Kalenderwoche vierzig"),
        ("§ 3 Abs. 2", "Paragraf drei Absatz zwei"),
        ("S. 4", "Seite vier"),
        ("inkl. MwSt.", "inklusive Mehrwertsteuer"),
        # Symbols, links and markup
        ("Müller & Söhne", "Müller und Söhne"),
        ("siehe https://example.com/a?b=1", "siehe Link"),
        ("an anna@example.org", "an anna at example Punkt org"),
        ("**Wichtig:** heute", "Wichtig: heute"),
        ("[Angebot](https://example.com)", "Angebot"),
        ("Danke 😀👍", "Danke"),
        ("und/oder", "und oder"),
        ("A  \u00a0 B", "A B"),
    ],
)
def test_normalize_german(text: str, spoken: str) -> None:
    assert normalize(text, "de") == spoken


def test_full_sentence() -> None:
    text = "Am Fr., 3.10.2026 um 14:30 Uhr trifft sich das Team, z. B. in Raum 2."

    assert normalize(text, "de") == (
        "Am Freitag, dritter Oktober zweitausendsechsundzwanzig um vierzehn Uhr dreißig "
        "trifft sich das Team, zum Beispiel in Raum zwei."
    )


def test_text_without_numbers_is_unchanged() -> None:
    text = "Guten Morgen, hier ist deine Zusammenfassung."

    assert normalize(text, "de") == text

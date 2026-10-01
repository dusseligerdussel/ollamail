"""Text preparation for speech: normalise, segment into sentences, split long pieces.

Engines read digits, dates and abbreviations poorly or not at all, so they are spelled
out here: ``am 3.10.2026 um 14:30 Uhr`` becomes ``am dritten Oktober zweitausendsechs-
undzwanzig um vierzehn Uhr dreißig``. The rules cover what typically appears in mail
digests (dates, times, amounts, percentages, common abbreviations); everything else is
left to the engine. Pure functions without I/O, so they are cheap to test.
"""

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from num2words import num2words

from app.ai.tts.types import Language

# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


def _digits(number: str, lang: Language) -> str:
    """Read digit by digit, e.g. for phone numbers and IDs."""
    return " ".join(num2words(int(digit), lang=lang) for digit in number)


def cardinal(number: int | str, lang: Language) -> str:
    try:
        # num2words writes "one thousand, two hundred"; the comma would become a pause.
        return str(num2words(int(number), lang=lang)).replace(",", "")
    except (OverflowError, NotImplementedError, ValueError):
        return _digits(str(number).lstrip("-"), lang)


def ordinal(number: int | str, lang: Language) -> str:
    return str(num2words(int(number), lang=lang, to="ordinal"))


def year(number: int | str, lang: Language) -> str:
    return str(num2words(int(number), lang=lang, to="year"))


def _de_ein(number: int | str) -> str:
    """German cardinal before a noun: ``1 Euro`` is ``ein Euro``, not ``eins Euro``."""
    words = cardinal(number, "de")
    return words[:-1] if words.endswith("eins") else words


def _integer(number: str, lang: Language) -> str:
    """A bare integer: years read as years, phone numbers and long IDs digit by digit."""
    if (len(number) > 1 and number.startswith("0")) or len(number) > 9:
        return _digits(number, lang)
    if len(number) == 4 and 1100 <= int(number) <= 2099:
        return year(number, lang)
    return cardinal(number, lang)


def _decimal(integer: str, fraction: str, lang: Language) -> str:
    separator = " Komma " if lang == "de" else " point "
    return cardinal(integer, lang) + separator + _digits(fraction, lang)


# ---------------------------------------------------------------------------
# Language data
# ---------------------------------------------------------------------------

MONTHS: dict[Language, tuple[str, ...]] = {
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

# Abbreviation -> spoken form. Dots and the space between parts are optional ("z.B.",
# "z. B."), matching is case-sensitive and only at word boundaries.
ABBREVIATIONS: dict[Language, dict[str, str]] = {
    "de": {
        "z. B.": "zum Beispiel",
        "d. h.": "das heißt",
        "u. a.": "unter anderem",
        "u. U.": "unter Umständen",
        "o. Ä.": "oder Ähnliches",
        "i. d. R.": "in der Regel",
        "z. T.": "zum Teil",
        "z. Hd.": "zu Händen",
        "usw.": "und so weiter",
        "bzw.": "beziehungsweise",
        "bzgl.": "bezüglich",
        "ca.": "circa",
        "evtl.": "eventuell",
        "ggf.": "gegebenenfalls",
        "inkl.": "inklusive",
        "exkl.": "exklusive",
        "zzgl.": "zuzüglich",
        "vgl.": "vergleiche",
        "etc.": "et cetera",
        "Nr.": "Nummer",
        "Tel.": "Telefon",
        "Dr.": "Doktor",
        "Prof.": "Professor",
        "Hr.": "Herr",
        "Hrn.": "Herrn",
        "Str.": "Straße",
        "Abs.": "Absatz",
        "Std.": "Stunden",
        "Min.": "Minuten",
        "Mio.": "Millionen",
        "Mrd.": "Milliarden",
        "Tsd.": "Tausend",
        "MwSt.": "Mehrwertsteuer",
        "KW": "Kalenderwoche",
        "Jh.": "Jahrhundert",
        "max.": "maximal",
        "allg.": "allgemein",
        "Anh.": "Anhang",
    },
    "en": {
        "e. g.": "for example",
        "i. e.": "that is",
        "etc.": "et cetera",
        "vs.": "versus",
        "approx.": "approximately",
        "dept.": "department",
        "Mr.": "Mister",
        "Mrs.": "Missus",
        "Ms.": "Miz",
        "Dr.": "Doctor",
        "Prof.": "Professor",
        "Jr.": "Junior",
        "Sr.": "Senior",
        "a. m.": "a m",
        "p. m.": "p m",
    },
}

WEEKDAYS_DE = {
    "Mo": "Montag",
    "Di": "Dienstag",
    "Mi": "Mittwoch",
    "Do": "Donnerstag",
    "Fr": "Freitag",
    "Sa": "Samstag",
    "So": "Sonntag",
}

WORDS: dict[Language, dict[str, str]] = {
    "de": {
        "and": "und",
        "percent": "Prozent",
        "to": "bis",
        "minus": "minus",
        "link": "Link",
        "at": "at",
        "dot": "Punkt",
        "section": "Paragraf",
        "celsius": "Grad Celsius",
        "number": "Nummer",
        "page": "Seite",
    },
    "en": {
        "and": "and",
        "percent": "percent",
        "to": "to",
        "minus": "minus",
        "link": "link",
        "at": "at",
        "dot": "dot",
        "section": "section",
        "celsius": "degrees Celsius",
        "number": "number",
        "page": "page",
    },
}

# (symbol, singular, plural, cent singular, cent plural) per language.
CURRENCIES: dict[Language, dict[str, tuple[str, str, str, str]]] = {
    "de": {
        "€": ("Euro", "Euro", "Cent", "Cent"),
        "EUR": ("Euro", "Euro", "Cent", "Cent"),
        "$": ("Dollar", "Dollar", "Cent", "Cent"),
        "USD": ("Dollar", "Dollar", "Cent", "Cent"),
        "£": ("Pfund", "Pfund", "Penny", "Pence"),
        "CHF": ("Franken", "Franken", "Rappen", "Rappen"),
    },
    "en": {
        "€": ("euro", "euros", "cent", "cents"),
        "EUR": ("euro", "euros", "cent", "cents"),
        "$": ("dollar", "dollars", "cent", "cents"),
        "USD": ("dollar", "dollars", "cent", "cents"),
        "£": ("pound", "pounds", "penny", "pence"),
        "CHF": ("franc", "francs", "centime", "centimes"),
    },
}

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile("[​-‍⁠﻿]")
_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>()\"']+", re.IGNORECASE)
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|`)(?=\S)(.+?)(?<=\S)\1")
_BULLET = re.compile(r"^\s*(?:[-*•\u2013·]|\d{1,2}[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_SENTENCE_END = re.compile(r"[.!?…:;]$")

_CURRENCY_SYMBOLS = r"€|\$|£|EUR|USD|CHF"


def _abbreviation_pattern(abbreviation: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in abbreviation.split(" ")]
    body = r"\s?".join(parts)
    end = "" if abbreviation.endswith(".") else r"\b"
    return re.compile(rf"(?<![\w.]){body}{end}")


_ABBREVIATION_PATTERNS: dict[Language, list[tuple[re.Pattern[str], str]]] = {
    lang: [(_abbreviation_pattern(short), long) for short, long in table.items()]
    for lang, table in ABBREVIATIONS.items()
}


def _plain_number(value: str) -> str:
    """``1.234``/``1,234``/``1 234`` -> ``1234`` (thousands separators only)."""
    return re.sub(r"[.,\u202f ]", "", value)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

Rule = Callable[[str, Language], str]


def _cleanup(text: str, lang: Language) -> str:
    text = unicodedata.normalize("NFC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace("\u00a0", " ").replace("\u2009", " ")
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_EMPHASIS.sub(r"\2", text)
    # Emoji and other pictographs are not speakable.
    text = "".join(char for char in text if unicodedata.category(char) != "So" or char in "°©®")
    return text


def _urls_and_emails(text: str, lang: Language) -> str:
    words = WORDS[lang]
    text = _URL.sub(words["link"], text)

    def email(match: re.Match[str]) -> str:
        local, domain = match.groups()
        spoken_domain = f" {words['dot']} ".join(domain.split("."))
        return f"{local} {words['at']} {spoken_domain}"

    return _EMAIL.sub(email, text)


def _abbreviations(text: str, lang: Language) -> str:
    if lang == "de":
        weekdays = "|".join(WEEKDAYS_DE)
        # "Mo., 3.10." / "Mo.-Fr." -> weekday; "Fr. Meier" -> Frau.
        text = re.sub(
            rf"(?<![\w.])({weekdays})\.(?=,?\s*\d|\s*[-\u2013]\s*(?:{weekdays})\.)",
            lambda m: WEEKDAYS_DE[m.group(1)],
            text,
        )
        # Second day of a range: "Montag-Fr."
        text = re.sub(
            rf"((?:tag|woch)[-\u2013])({weekdays})\.",
            lambda m: m.group(1) + WEEKDAYS_DE[m.group(2)],
            text,
        )
        text = re.sub(r"(?<![\w.])Fr\.(?=\s+[A-ZÄÖÜ])", "Frau", text)
        text = re.sub(r"(?<![\w.])S\.\s?(?=\d)", "Seite ", text)
    else:
        text = re.sub(r"(?<![\w.])No\.\s?(?=\d)", "number ", text)
        text = re.sub(r"(?<![\w.])p\.\s?(?=\d)", "page ", text)
    for pattern, spoken in _ABBREVIATION_PATTERNS[lang]:
        text = pattern.sub(spoken, text)
    return text


def _month_from(name: str, lang: Language) -> int | None:
    """Month number for a full or abbreviated (``Oct``, ``Okt.``) month name."""
    name = name.rstrip(".").lower()
    for index, month in enumerate(MONTHS[lang], start=1):
        full = month.lower()
        if name == full or (len(name) >= 3 and full.startswith(name)):
            return index
    if lang == "de" and name == "okt":
        return 10
    return None


def _de_ordinal_for_date(day: int, preceding: str) -> str:
    # After a preposition the dative ("am dritten"), otherwise nominative ("dritter").
    base = ordinal(day, "de")
    dative = preceding.lower() in {"am", "vom", "zum", "ab", "bis", "seit", "dem", "beim"}
    return base + ("n" if dative else "r")


def _spoken_date(day: int, month: int, year_: int | None, lang: Language, preceding: str) -> str:
    month_name = MONTHS[lang][month - 1]
    if lang == "de":
        spoken = f"{_de_ordinal_for_date(day, preceding)} {month_name}"
    else:
        spoken = f"{month_name} {ordinal(day, 'en')}"
    if year_ is not None:
        spoken += (" " if lang == "de" else ", ") + year(year_, lang)
    return spoken


def _full_year(value: str) -> int:
    return int(value) + 2000 if len(value) == 2 else int(value)


def _previous_word(text: str, position: int) -> str:
    match = re.search(r"(\w+)\W*$", text[:position])
    return match.group(1) if match else ""


def _dates(text: str, lang: Language) -> str:
    def iso(match: re.Match[str]) -> str:
        year_, month, day = (int(group) for group in match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return _spoken_date(day, month, year_, lang, _previous_word(text, match.start()))

    text = re.sub(r"\b(\d{4})-(\d{2})-(\d{2})\b", iso, text)

    if lang == "de":
        months = "|".join(MONTHS["de"]) + "|Jan|Feb|Mär|Apr|Jun|Jul|Aug|Sep|Sept|Okt|Nov|Dez"

        def numeric(match: re.Match[str]) -> str:
            day, month = int(match.group(1)), int(match.group(2))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                return match.group(0)
            year_ = _full_year(match.group(3)) if match.group(3) else None
            return _spoken_date(day, month, year_, lang, _previous_word(text, match.start()))

        # 3.10.2026, 03.10.26, 3.10.
        text = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2}(?!\d))?", numeric, text)

        def named(match: re.Match[str]) -> str:
            month = _month_from(match.group(2), "de")
            day = int(match.group(1))
            if month is None or not 1 <= day <= 31:
                return match.group(0)
            year_ = int(match.group(3)) if match.group(3) else None
            return _spoken_date(day, month, year_, lang, _previous_word(text, match.start()))

        # 3. Oktober 2026, 3. Okt.
        text = re.sub(rf"\b(\d{{1,2}})\.\s*({months})\b\.?(?:\s+(\d{{4}}))?", named, text)
        return text

    months = "|".join(MONTHS["en"]) + "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"

    def month_first(match: re.Match[str]) -> str:
        month = _month_from(match.group(1), "en")
        day = int(match.group(2))
        if month is None or not 1 <= day <= 31:
            return match.group(0)
        year_ = int(match.group(3)) if match.group(3) else None
        return _spoken_date(day, month, year_, "en", "")

    # October 3, 2026 / Oct. 3rd
    text = re.sub(
        rf"\b({months})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", month_first, text
    )

    def day_first(match: re.Match[str]) -> str:
        month = _month_from(match.group(2), "en")
        day = int(match.group(1))
        if month is None or not 1 <= day <= 31:
            return match.group(0)
        spoken = f"the {ordinal(day, 'en')} of {MONTHS['en'][month - 1]}"
        if match.group(3):
            spoken += " " + year(match.group(3), "en")
        return spoken

    # 3 October 2026 / 3rd of Oct
    return re.sub(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({months})\b\.?(?:\s+(\d{{4}}))?",
        day_first,
        text,
    )


def _times(text: str, lang: Language) -> str:
    if lang == "de":

        def de_time(match: re.Match[str]) -> str:
            hour, minute = int(match.group(1)), int(match.group(2))
            spoken = f"{_de_ein(hour)} Uhr"
            return spoken + (f" {cardinal(minute, 'de')}" if minute else "")

        text = re.sub(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*Uhr\b", de_time, text)
        text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", de_time, text)
        return re.sub(r"\b([01]?\d|2[0-4])\s*Uhr\b", lambda m: f"{_de_ein(m.group(1))} Uhr", text)

    def en_clock(hour: int, minute: int, suffix: str) -> str:
        spoken = cardinal(hour, "en")
        if minute:
            spoken += " " + (
                f"oh {cardinal(minute, 'en')}" if minute < 10 else cardinal(minute, "en")
            )
        elif not suffix:
            spoken += " o'clock"
        return spoken + (f" {suffix}" if suffix else "")

    def twelve_hour(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        suffix = "a m" if match.group(3).lower() == "a" else "p m"
        return en_clock(hour, minute, suffix)

    text = re.sub(
        r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s?m\b", twelve_hour, text, flags=re.I
    )

    def twenty_four_hour(match: re.Match[str]) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        suffix = "a m" if hour < 12 else "p m"
        return en_clock(hour % 12 or 12, minute, suffix)

    return re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", twenty_four_hour, text)


def _amount(integer: str, cents: str | None, symbol: str, lang: Language) -> str:
    unit, units, cent, cents_word = CURRENCIES[lang][symbol]
    value = int(_plain_number(integer))
    if lang == "de":
        spoken = f"{_de_ein(value)} {unit if value == 1 else units}"
    else:
        spoken = f"{cardinal(value, 'en')} {unit if value == 1 else units}"
    cent_value = int(cents) if cents else 0
    if cent_value:
        if lang == "de":
            # "zwölf Euro fünfzig" is how amounts are said in German.
            spoken += f" {cardinal(cent_value, 'de')}"
        else:
            word = cent if cent_value == 1 else cents_word
            spoken += f" and {cardinal(cent_value, 'en')} {word}"
    return spoken


def _currency(text: str, lang: Language) -> str:
    # In German "," separates cents, in English ".".
    cents_sep = "," if lang == "de" else r"\."
    thousands = r"\d{1,3}(?:[.\u202f ]\d{3})+" if lang == "de" else r"\d{1,3}(?:[,\u202f ]\d{3})+"
    number = rf"({thousands}|\d+)(?:{cents_sep}(\d{{2}}|-{{1,2}}))?"

    def after(match: re.Match[str]) -> str:
        cents = match.group(2) if match.group(2) and "-" not in match.group(2) else None
        return _amount(match.group(1), cents, match.group(3), lang)

    def before(match: re.Match[str]) -> str:
        cents = match.group(3) if match.group(3) and "-" not in match.group(3) else None
        return _amount(match.group(2), cents, match.group(1), lang)

    text = re.sub(rf"{number}\s?({_CURRENCY_SYMBOLS})(?![A-Za-z])", after, text)
    return re.sub(rf"(?<![A-Za-z])({_CURRENCY_SYMBOLS})\s?{number}", before, text)


def _percent(text: str, lang: Language) -> str:
    sep = "," if lang == "de" else r"\."

    def spoken(match: re.Match[str]) -> str:
        integer, fraction = match.group(1), match.group(2)
        value = _decimal(integer, fraction, lang) if fraction else cardinal(integer, lang)
        if lang == "de" and not fraction:
            value = _de_ein(integer)
        return f"{value} {WORDS[lang]['percent']}"

    return re.sub(rf"\b(\d+)(?:{sep}(\d+))?\s?(?:%|Prozent\b|percent\b)", spoken, text)


def _ordinals(text: str, lang: Language) -> str:
    if lang == "en":
        return re.sub(
            r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(m.group(1), "en"), text, flags=re.I
        )
    return text


def _symbols(text: str, lang: Language) -> str:
    words = WORDS[lang]
    text = re.sub(r"\s?°\s?C\b", f" {words['celsius']}", text)
    text = re.sub(r"§\s?(?=\d)", f"{words['section']} ", text)
    text = re.sub(r"\s&\s", f" {words['and']} ", text)
    text = re.sub(r"(?<=\w)&(?=\w)", f" {words['and']} ", text)
    text = re.sub(r"(?<=\s)#(?=\d)", f"{words['number']} ", text)
    text = re.sub(r"(?<=[^\W\d])/(?=[^\W\d])", " ", text)
    # Negative numbers.
    text = re.sub(r"(?:(?<=\s)|^)[-\u2212](?=\d)", f"{words['minus']} ", text)
    # Dashes and other separators become pauses.
    text = re.sub(r"\s+[\u2013\u2014-]\s+", ", ", text)
    return re.sub(r"[|~^*_#<>{}\[\]\\]", " ", text)


def _phone_numbers(text: str, lang: Language) -> str:
    """``+49 171 1234567``, ``030/123456``: digit by digit."""

    def spoken(match: re.Match[str]) -> str:
        number = match.group(0)
        prefix = "plus " if number.startswith("+") else ""
        return prefix + _digits(re.sub(r"\D", "", number), lang)

    return re.sub(r"(?<![\w.,])(?:\+|0)\d[\d /-]{4,}\d(?![\w])", spoken, text)


def _ranges(text: str, lang: Language) -> str:
    """``3-5``, ``9-17 Uhr``, ``14:00-15:30``; not reference numbers like ``Nr. 2026-17``."""
    words = WORDS[lang]

    def spoken(match: re.Match[str]) -> str:
        if _previous_word(text, match.start()).lower() in {"nummer", "number", "no"}:
            return match.group(0)
        return f"{match.group(1)} {words['to']} {match.group(2)}"

    return re.sub(r"\b(\d{1,4})\s?[-\u2013]\s?(\d{1,4})\b", spoken, text)


def _numbers(text: str, lang: Language) -> str:
    decimal_sep = "," if lang == "de" else r"\."
    thousands_sep = r"\." if lang == "de" else ","

    def spoken(match: re.Match[str]) -> str:
        integer, fraction = match.group(1), match.group(2)
        if fraction:
            return _decimal(_plain_number(integer), fraction, lang)
        if not integer.isdigit():
            return cardinal(_plain_number(integer), lang)
        return _integer(integer, lang)

    return re.sub(
        rf"(\d{{1,3}}(?:{thousands_sep}\d{{3}})+(?!\d)|\d+)(?:{decimal_sep}(\d+))?", spoken, text
    )


def _whitespace(text: str, lang: Language) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


RULES: tuple[Rule, ...] = (
    _cleanup,
    _urls_and_emails,
    _abbreviations,
    _dates,
    _phone_numbers,
    _ranges,
    _times,
    _currency,
    _percent,
    _ordinals,
    _symbols,
    _numbers,
    _whitespace,
)


def normalize(text: str, lang: Language) -> str:
    """Make one line or paragraph speakable (numbers, dates, abbreviations, symbols)."""
    for rule in RULES:
        text = rule(text, lang)
    return text


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """A piece of text for one engine call and the pause that follows it."""

    text: str
    pause_after: float


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines; list items and headings become paragraphs of their own."""
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        is_item = bool(_BULLET.match(line) or _HEADING.match(line))
        if is_item:
            flush()
            line = _HEADING.sub("", _BULLET.sub("", line))
        current.append(line)
        if is_item:
            flush()
    flush()
    return paragraphs


def split_sentences(text: str) -> list[str]:
    """Split normalised text after ``.``, ``!``, ``?`` and ``…``.

    Runs after :func:`normalize`, so the dots of abbreviations and numbers are already
    gone. A single capital letter before the dot (an initial) does not end a sentence."""
    sentences: list[str] = []
    start = 0
    for match in re.finditer(r"[.!?…]+[\"'»«“”)]*(?=\s+|$)", text):
        end = match.end()
        before = text[start : match.start()]
        if match.group(0).startswith(".") and re.search(r"(?:^|\s)[A-ZÄÖÜ]$", before):
            continue
        sentences.append(text[start:end].strip())
        start = end
    rest = text[start:].strip()
    if rest:
        sentences.append(rest)
    return [sentence for sentence in sentences if any(char.isalnum() for char in sentence)]


def split_long(sentence: str, max_chars: int) -> list[str]:
    """Split a sentence longer than ``max_chars`` at clause boundaries, then at spaces."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    current = ""
    for clause in re.split(r"(?<=[,;:\u2013])\s+", sentence):
        candidate = f"{current} {clause}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = clause
        while len(current) > max_chars:
            cut = current.rfind(" ", 0, max_chars + 1)
            cut = cut if cut > 0 else max_chars
            pieces.append(current[:cut].strip())
            current = current[cut:].strip()
    if current:
        pieces.append(current)
    return pieces


def segment(
    text: str,
    lang: Language,
    *,
    max_chars: int = 400,
    sentence_pause: float = 0.35,
    paragraph_pause: float = 0.8,
) -> list[Segment]:
    """Turn free text (plain or light Markdown) into speakable pieces with pauses."""
    segments: list[Segment] = []
    for paragraph in _paragraphs(text):
        normalized = normalize(paragraph, lang)
        if not normalized:
            continue
        if not _SENTENCE_END.search(normalized):
            normalized += "."
        sentences = split_sentences(normalized)
        for index, sentence in enumerate(sentences):
            pieces = split_long(sentence, max_chars)
            for piece_index, piece in enumerate(pieces):
                last_piece = piece_index == len(pieces) - 1
                pause = sentence_pause if last_piece else sentence_pause / 2
                if last_piece and index == len(sentences) - 1:
                    pause = paragraph_pause
                segments.append(Segment(piece, pause))
    if segments:
        segments[-1] = Segment(segments[-1].text, 0.0)
    return segments

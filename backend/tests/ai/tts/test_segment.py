from app.ai.tts.normalize import Segment, segment, split_long, split_sentences


def test_split_sentences() -> None:
    text = "Erster Satz. Zweiter Satz! Dritter? Vierter… Ende"

    assert split_sentences(text) == [
        "Erster Satz.",
        "Zweiter Satz!",
        "Dritter?",
        "Vierter…",
        "Ende",
    ]


def test_initials_do_not_end_a_sentence() -> None:
    assert split_sentences("Rechnung von A. Schmidt liegt vor. Danke.") == [
        "Rechnung von A. Schmidt liegt vor.",
        "Danke.",
    ]


def test_quotes_stay_with_their_sentence() -> None:
    assert split_sentences("Er sagte „Ja.“ Dann ging er.") == ["Er sagte „Ja.“", "Dann ging er."]


def test_split_long_prefers_clause_boundaries() -> None:
    sentence = "eins zwei drei, vier fünf sechs, sieben acht neun"

    assert split_long(sentence, 20) == ["eins zwei drei,", "vier fünf sechs,", "sieben acht neun"]


def test_split_long_falls_back_to_spaces() -> None:
    pieces = split_long("a" * 10 + " " + "b" * 10 + " " + "c" * 30, 15)

    assert pieces == ["a" * 10, "b" * 10, "c" * 15, "c" * 15]
    assert all(len(piece) <= 15 for piece in pieces)


def test_segment_paragraphs_lists_and_pauses() -> None:
    text = """# Guten Morgen

Du hast 3 neue Mails. Die wichtigste kommt von Hr. Weber.

- Rechnung über 49,90 €
- Termin am 6.10. um 9:30 Uhr
"""

    assert segment(text, "de", sentence_pause=0.3, paragraph_pause=0.8) == [
        Segment("Guten Morgen.", 0.8),
        Segment("Du hast drei neue Mails.", 0.3),
        Segment("Die wichtigste kommt von Herr Weber.", 0.8),
        Segment("Rechnung über neunundvierzig Euro neunzig.", 0.8),
        Segment("Termin am sechsten Oktober um neun Uhr dreißig.", 0.0),
    ]


def test_segment_splits_long_sentences() -> None:
    sentence = ", ".join(["ein recht langer Satzteil"] * 30) + "."

    segments = segment(sentence, "de", max_chars=100, sentence_pause=0.4)

    assert len(segments) > 1
    assert all(len(s.text) <= 100 for s in segments)
    # Shorter pause inside a sentence, none at the very end.
    assert {s.pause_after for s in segments[:-1]} == {0.2}
    assert segments[-1].pause_after == 0.0
    joined = " ".join(s.text for s in segments)
    assert joined == sentence


def test_segment_ignores_empty_and_unspeakable_text() -> None:
    assert segment("", "en") == []
    assert segment("\n\n  \n- \n😀\n", "en") == []

import pytest

from app.mail.language import detect_language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Vielen Dank für die schnelle Antwort, wir melden uns morgen bei Ihnen.", "de"),
        ("Thank you for the quick reply, we will get back to you tomorrow.", "en"),
        ("Merci pour votre réponse rapide, nous vous recontacterons demain.", "fr"),
    ],
)
def test_detects_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


@pytest.mark.parametrize("text", ["", "ok", "12345 67890", "https://example.com/very/long/path"])
def test_too_little_text_gives_none(text: str) -> None:
    assert detect_language(text) is None


def test_quoted_lines_are_ignored() -> None:
    text = "Danke, passt so.\n" + "> This is a long English quote that would dominate.\n" * 20

    assert detect_language(text) in {None, "de"}

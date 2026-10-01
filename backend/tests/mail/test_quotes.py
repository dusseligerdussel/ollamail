import pytest

from app.mail.quotes import split_reply


def test_plain_text_without_quote() -> None:
    parts = split_reply("Hello\n\nJust a note.\n")

    assert parts.main == "Hello\n\nJust a note."
    assert parts.quoted is None
    assert parts.signature is None


@pytest.mark.parametrize(
    "attribution",
    [
        "On Mon, 31 Aug 2026 at 18:20, Max Mustermann <max@example.com> wrote:",
        "Am 31.08.2026 um 18:20 schrieb Max Mustermann <max@example.com>:",
        "Le lun. 31 août 2026 à 18:20, Max <max@example.com> a écrit :",
        "-----Original Message-----",
        "-----Ursprüngliche Nachricht-----",
        "---------- Forwarded message ---------",
    ],
)
def test_attribution_lines_start_the_quote(attribution: str) -> None:
    parts = split_reply(f"New text.\n\n{attribution}\nold text\n")

    assert parts.main == "New text."
    assert parts.quoted is not None
    assert parts.quoted.startswith(attribution.strip())


def test_wrapped_attribution_line() -> None:
    parts = split_reply(
        "Ok.\n\nOn Mon, 31 Aug 2026 at 18:20, Max Mustermann\n<max@example.com> wrote:\n> x"
    )

    assert parts.main == "Ok."


def test_outlook_header_block_in_english() -> None:
    text = "Sounds good.\n\nFrom: Max <max@example.com>\nSent: Monday\nTo: Erika\nSubject: x\n\nold"

    assert split_reply(text).main == "Sounds good."


def test_from_line_without_following_headers_is_content() -> None:
    text = "Let me explain.\nFrom: the beginning, this was planned.\nThat is all."

    assert split_reply(text).main == text


def test_inline_replies_are_kept() -> None:
    text = "> question one?\nanswer one\n> question two?\nanswer two"

    parts = split_reply(text)

    assert parts.main == text
    assert parts.quoted is None


def test_trailing_quote_block_without_attribution() -> None:
    parts = split_reply("my answer\n\n> old\n>\n> older\n")

    assert parts.main == "my answer"
    assert parts.quoted == "> old\n>\n> older"


def test_signature_delimiter_with_and_without_trailing_space() -> None:
    for delimiter in ("-- ", "--"):
        parts = split_reply(f"Body text\n{delimiter}\nName\nCompany")
        assert parts.main == "Body text"
        assert parts.signature == "Name\nCompany"


def test_long_tail_after_delimiter_is_not_a_signature() -> None:
    tail = "\n".join(f"line {i}" for i in range(30))

    parts = split_reply(f"intro\n--\n{tail}")

    assert parts.signature is None
    assert "line 29" in parts.main


@pytest.mark.parametrize(
    "line", ["Sent from my iPhone", "Von meinem iPhone gesendet", "Get Outlook for Android"]
)
def test_mobile_signatures(line: str) -> None:
    parts = split_reply(f"Short answer.\n\n{line}")

    assert parts.main == "Short answer."
    assert parts.signature == line


def test_only_forwarded_content_stays_readable() -> None:
    text = "---------- Forwarded message ---------\nFrom: a@example.com\n\nForwarded body"

    parts = split_reply(text)

    assert parts.main == text
    assert parts.quoted is None

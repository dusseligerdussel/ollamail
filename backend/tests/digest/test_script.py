"""Fixed texts of the script (counts, bulk mail, todos) and the spoken form."""

import uuid
from datetime import UTC, date, datetime

from app.ai.tts.normalize import segment
from app.digest import script, texts
from app.digest.content import DigestContent, MailItem, TodoItem
from app.todos.models import TodoPriority

FRIDAY = date(2026, 10, 2)


def mail(category: str | None, priority: int = 2) -> MailItem:
    return MailItem(
        message_id=uuid.uuid4(),
        mailbox_id=uuid.uuid4(),
        sender="Max Example",
        subject="Report",
        body="",
        received_at=datetime(2026, 10, 2, 6, tzinfo=UTC),
        category=category,
        priority=priority,
    )


def test_intro_counts_all_mails_and_the_important_ones() -> None:
    content = DigestContent(
        mails=[mail("important"), mail("action_required"), mail("info"), mail(None, 1)],
        more_mails=2,
        bulk={"newsletter": ["Weekly News", "Daily Deals"]},
    )

    assert texts.intro(FRIDAY, content, "de") == (
        "Dein Überblick am Freitag, dem 2. Oktober 2026. Seit dem letzten Digest sind 8 neue "
        "Mails eingegangen, 3 davon sind wichtig."
    )
    assert texts.intro(FRIDAY, content, "en") == (
        "Your overview for Friday, October 2, 2026. Since the last digest, 8 new mails have "
        "arrived, 3 of them important."
    )


def test_intro_without_mail() -> None:
    assert texts.intro(FRIDAY, DigestContent(), "de").endswith(
        "Seit dem letzten Digest sind keine neuen Mails eingegangen."
    )
    assert texts.intro(FRIDAY, DigestContent(mails=[mail("info")]), "en").endswith(
        "one new mail has arrived."
    )


def test_bulk_mail_is_one_collective_sentence() -> None:
    groups = {
        "newsletter": ["Weekly News", "Daily Deals", "Weekly News", "Tech Digest", "Travel"],
        "notification": ["Shop"],
    }

    assert texts.bulk(groups, "de") == (
        "Außerdem kamen 5 Newsletter unter anderem von Weekly News, Daily Deals und Tech "
        "Digest und eine automatische Benachrichtigung von Shop."
    )
    assert texts.bulk(groups, "en") == (
        "Also: 5 newsletters, among others from Weekly News, Daily Deals and Tech Digest and "
        "one automated notification from Shop."
    )
    assert texts.bulk({}, "en") is None


def test_todos_by_due_date() -> None:
    content = DigestContent(
        new_todos=[TodoItem("Send the report.", None, TodoPriority.HIGH)],
        due_todos=[
            TodoItem("Pay invoice", date(2026, 9, 30), TodoPriority.NORMAL),
            TodoItem("Book room", FRIDAY, TodoPriority.NORMAL),
            TodoItem("Call Anna", date(2026, 10, 3), TodoPriority.LOW),
        ],
    )

    assert texts.todos(content, FRIDAY, "de") == [
        "Neue Aufgaben: Send the report.",
        "Überfällig: Pay invoice.",
        "Heute fällig: Book room.",
        "Morgen fällig: Call Anna.",
    ]


def test_script_and_spoken_text() -> None:
    content = DigestContent(mails=[mail("important")], bulk={"newsletter": ["News"]})

    text = script.build(content, "Max needs the report by Friday [1].", today=FRIDAY, language="en")

    assert text.startswith("# Digest for Friday, October 2, 2026\n\nYour overview")
    assert "Max needs the report by Friday [1]." in text
    spoken = script.spoken(text)
    assert not spoken.startswith("#")
    assert "[1]" not in spoken
    assert "Max needs the report by Friday." in spoken
    assert spoken.endswith("That's your digest.")


def test_spoken_text_is_speakable() -> None:
    content = DigestContent(mails=[mail("important")])
    text = script.build(content, "Anna bittet um Rückmeldung [1].", today=FRIDAY, language="de")

    spoken = " ".join(piece.text for piece in segment(script.spoken(text), "de"))

    assert "am Freitag, dem zweiten Oktober zweitausendsechsundzwanzig." in spoken
    assert "ist eine neue Mail eingegangen, sie ist wichtig." in spoken
    assert "[" not in spoken and "#" not in spoken

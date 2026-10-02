from app.mail.api.autodiscovery import Hint, Source, domain_of, suggest
from app.mail.models import MailboxType


def only_imap(type: MailboxType) -> bool:
    return type is MailboxType.IMAP


def everything(_: MailboxType) -> bool:
    return True


def test_domain_of() -> None:
    assert domain_of(" Erika@Example.ORG. ") == "example.org"
    assert domain_of("erika@localhost") is None
    assert domain_of("no-at-sign.example.org") is None
    assert domain_of("erika@") is None


def test_known_provider() -> None:
    [suggestion] = suggest("erika@googlemail.com", only_imap)

    assert suggestion.type is MailboxType.IMAP
    assert suggestion.provider_settings == {
        "host": "imap.gmail.com",
        "port": 993,
        "security": "tls",
    }
    assert suggestion.source is Source.KNOWN
    assert suggestion.hints == (Hint.APP_PASSWORD,)


def test_native_provider_first_once_registered() -> None:
    types = [s.type for s in suggest("erika@outlook.de", everything)]
    assert types == [MailboxType.GRAPH, MailboxType.IMAP]
    assert suggest("erika@gmail.com", everything)[0].type is MailboxType.GMAIL


def test_unknown_domain_is_guessed() -> None:
    suggestions = suggest("erika@example.org", only_imap)

    assert [s.provider_settings["host"] for s in suggestions] == [
        "imap.example.org",
        "mail.example.org",
    ]
    assert all(s.source is Source.GUESS and s.hints == (Hint.GUESSED,) for s in suggestions)


def test_nothing_without_registered_provider() -> None:
    assert suggest("erika@example.org", lambda _: False) == []
    assert suggest("invalid", everything) == []

"""The digest script: Markdown with ``[n]`` references, and its spoken form.

Layout: a title heading, the introduction with counts, the model's summary, collective
sentences for further mails and bulk mail, the todos, a closing line. References point
into ``Digest.references`` (``n`` -> message ID); the spoken text drops them.
"""

import re
from datetime import date

from app.digest import texts
from app.digest.content import DigestContent
from app.digest.texts import DigestLanguage

_REFERENCE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
_HEADING = re.compile(r"^\s*#{1,6}\s+.*$", re.MULTILINE)


def build(content: DigestContent, summary: str, *, today: date, language: DigestLanguage) -> str:
    paragraphs = [f"# {texts.title(today, language)}", texts.intro(today, content, language)]
    if summary:
        paragraphs.append(summary)
    if content.more_mails:
        paragraphs.append(texts.more_mails(content.more_mails, language))
    bulk = texts.bulk(content.bulk, language)
    if bulk:
        paragraphs.append(bulk)
    paragraphs += texts.todos(content, today, language)
    paragraphs.append(texts.closing(language))
    return "\n\n".join(paragraphs) + "\n"


def spoken(script: str) -> str:
    """Text for the TTS: without the title heading and the reference marks."""
    text = _HEADING.sub("", script)
    return _REFERENCE.sub("", text).strip()

"""The private podcast feed: RSS 2.0 with the iTunes podcast namespace.

Every URL in the feed carries the feed token, because podcast apps cannot sign in. The
feed asks directories not to list it (``itunes:block``); the token is the only secret, so
revoking it (or creating a new one) cuts off the feed and all audio URLs at once.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from xml.etree import ElementTree as ET

from app.digest import script
from app.digest.models import Digest
from app.digest.texts import DigestLanguage

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)

MEDIA_TYPE = "application/rss+xml; charset=utf-8"
# Podcast apps support MP3 everywhere, Ogg/Opus not; MP3 is preferred when both exist.
ENCLOSURE_ORDER = ("mp3", "opus")
ENCLOSURE_TYPES = {"mp3": "audio/mpeg", "opus": "audio/ogg"}

CHANNEL_TEXTS: dict[DigestLanguage, tuple[str, str]] = {
    "de": ("ollamail: Tägliche Zusammenfassung", "Deine privaten Mail-Zusammenfassungen."),
    "en": ("ollamail: Daily digest", "Your private mail summaries."),
}


@dataclass(frozen=True, slots=True)
class FeedLinks:
    """Absolute URLs: the web app, this feed, and a builder for audio URLs."""

    site: str
    feed: str
    audio_base: str
    image: str

    def audio(self, digest: Digest, fmt: str) -> str:
        return f"{self.audio_base}/{digest.id}.{fmt}"


def duration(seconds: float) -> str:
    """``HH:MM:SS`` for ``itunes:duration``."""
    total = max(0, round(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = text
    return element


def enclosure_format(digest: Digest) -> str | None:
    return next((fmt for fmt in ENCLOSURE_ORDER if fmt in digest.audio), None)


def render(
    digests: Sequence[Digest],
    *,
    links: FeedLinks,
    language: DigestLanguage,
    built_at: datetime,
) -> bytes:
    title, description = CHANNEL_TEXTS[language]
    rss = ET.Element("rss", {"version": "2.0"})
    channel = _sub(rss, "channel")
    _sub(channel, "title", title)
    _sub(channel, "link", links.site)
    _sub(channel, "description", description)
    _sub(channel, "language", language)
    _sub(channel, "lastBuildDate", format_datetime(built_at.astimezone(UTC), usegmt=True))
    _sub(channel, "ttl", "60")
    _sub(channel, f"{{{ATOM}}}link", href=links.feed, rel="self", type="application/rss+xml")
    image = _sub(channel, "image")
    _sub(image, "url", links.image)
    _sub(image, "title", title)
    _sub(image, "link", links.site)
    _sub(channel, f"{{{ITUNES}}}author", "ollamail")
    _sub(channel, f"{{{ITUNES}}}summary", description)
    _sub(channel, f"{{{ITUNES}}}image", href=links.image)
    _sub(channel, f"{{{ITUNES}}}category", text="News")
    _sub(channel, f"{{{ITUNES}}}explicit", "false")
    _sub(channel, f"{{{ITUNES}}}type", "episodic")
    # Private feed: keep it out of podcast directories.
    _sub(channel, f"{{{ITUNES}}}block", "Yes")

    for digest in digests:
        fmt = enclosure_format(digest)
        if fmt is None:
            continue
        published = (digest.generated_at or digest.created_at).astimezone(UTC)
        item = _sub(channel, "item")
        _sub(item, "title", digest.title)
        _sub(item, "description", script.spoken(digest.script or ""))
        _sub(item, "guid", f"ollamail-digest-{digest.id}", isPermaLink="false")
        _sub(item, "pubDate", format_datetime(published, usegmt=True))
        _sub(
            item,
            "enclosure",
            url=links.audio(digest, fmt),
            length=str(digest.audio[fmt]["size_bytes"]),
            type=ENCLOSURE_TYPES[fmt],
        )
        _sub(item, f"{{{ITUNES}}}duration", duration(digest.duration_seconds or 0))
        _sub(item, f"{{{ITUNES}}}explicit", "false")
        _sub(item, f"{{{ITUNES}}}episodeType", "full")

    ET.indent(rss)
    xml: bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml

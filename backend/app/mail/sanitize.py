"""HTML handling: safe HTML for display and HTML → plain text.

``sanitize_html`` runs server-side with ``nh3`` (Rust ``ammonia``, an allow-list
sanitiser). Scripts, styles, forms, frames, event handlers and dangerous URL schemes are
removed. External images are blocked by default because they are commonly used as
tracking pixels; ``cid:`` images are rewritten to attachment URLs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

import nh3

_URL_SCHEMES = frozenset({"http", "https", "mailto", "tel", "cid"})

# Inline CSS is common in mail HTML; only properties that cannot load resources or
# overlay the application UI are kept (no ``background``, ``position``, ``url()``).
_STYLE_PROPERTIES = frozenset(
    {
        "border",
        "border-bottom",
        "border-collapse",
        "border-color",
        "border-left",
        "border-radius",
        "border-right",
        "border-spacing",
        "border-style",
        "border-top",
        "border-width",
        "color",
        "font",
        "font-family",
        "font-size",
        "font-style",
        "font-weight",
        "height",
        "letter-spacing",
        "line-height",
        "margin",
        "margin-bottom",
        "margin-left",
        "margin-right",
        "margin-top",
        "max-width",
        "min-width",
        "padding",
        "padding-bottom",
        "padding-left",
        "padding-right",
        "padding-top",
        "text-align",
        "text-decoration",
        "text-transform",
        "vertical-align",
        "white-space",
        "width",
    }
)


def _allowed_attributes() -> dict[str, set[str]]:
    attributes = {tag: set(names) for tag, names in nh3.ALLOWED_ATTRIBUTES.items()}
    attributes["*"] = {"style", "dir", "lang", "title"}
    for tag, names in {
        "table": {"width", "border", "cellpadding", "cellspacing"},
        "td": {"width", "valign"},
        "th": {"width", "valign"},
        "font": {"color", "size"},
    }.items():
        attributes.setdefault(tag, set()).update(names)
    return attributes


_TAGS = nh3.ALLOWED_TAGS | {"font"}
_ATTRIBUTES = _allowed_attributes()

CidResolver = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class SafeHtml:
    html: str
    # Number of external images removed; the UI offers "load images" if > 0.
    blocked_images: int


def sanitize_html(
    html: str,
    *,
    allow_external_images: bool = False,
    resolve_cid: CidResolver | None = None,
) -> SafeHtml:
    """Return HTML that is safe to render inside the application.

    ``resolve_cid`` maps a ``Content-ID`` (without ``cid:``) to a URL for the inline
    attachment; unresolvable ``cid:`` images are dropped.
    """
    blocked = 0

    def filter_attribute(element: str, attribute: str, value: str) -> str | None:
        nonlocal blocked
        if element != "img" or attribute != "src":
            return value
        scheme = value.split(":", 1)[0].strip().lower() if ":" in value else ""
        if scheme == "cid":
            return resolve_cid(value[4:].strip("<> ")) if resolve_cid else None
        if scheme in {"http", "https"} or value.startswith("//"):
            if allow_external_images:
                return value
            blocked += 1
            return None
        # Relative URLs have no meaning in a mail; drop them.
        return None

    cleaned = nh3.clean(
        html,
        tags=set(_TAGS),
        attributes=_ATTRIBUTES,
        attribute_filter=filter_attribute,
        url_schemes=set(_URL_SCHEMES),
        filter_style_properties=set(_STYLE_PROPERTIES),
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    return SafeHtml(html=cleaned, blocked_images=blocked)


_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
)
_SKIP_TAGS = frozenset({"head", "script", "style", "title", "template", "noscript"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._line: list[str] = []
        self._skip = 0
        self._pre = 0
        self._quote = 0
        self._line_quote = 0

    def _flush(self) -> None:
        text = "".join(self._line)
        if not self._pre:
            text = " ".join(text.split())
        prefix = "> " * self._line_quote
        self.lines.append(f"{prefix}{text}".rstrip() if text else prefix.rstrip())
        self._line = []
        self._line_quote = self._quote

    def _block_boundary(self) -> None:
        if self._line and "".join(self._line).strip():
            self._flush()
        else:
            self._line = []
            self._line_quote = self._quote

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "br":
            self._flush()
            return
        if tag in _BLOCK_TAGS:
            self._block_boundary()
        if tag == "blockquote":
            self._quote += 1
            self._line_quote = self._quote
        elif tag == "pre":
            self._pre += 1
        elif tag == "li":
            self._line.append("- ")
        elif tag in {"td", "th"} and self._line:
            self._line.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in _BLOCK_TAGS:
            self._block_boundary()
        if tag == "blockquote":
            self._quote = max(0, self._quote - 1)
            self._line_quote = self._quote
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._pre:
            parts = data.split("\n")
            for part in parts[:-1]:
                self._line.append(part)
                self._flush()
            self._line.append(parts[-1])
        else:
            self._line.append(data)

    def text(self) -> str:
        self._block_boundary()
        result: list[str] = []
        for line in self.lines:
            stripped = line.strip("> ")
            # Collapse runs of empty lines into one.
            if not stripped and result and not result[-1].strip("> "):
                continue
            result.append(line.replace("\xa0", " "))
        return "\n".join(result).strip()


def html_to_text(html: str) -> str:
    """Readable plain text from HTML; ``<blockquote>`` becomes ``> `` quoting so quote
    detection works the same for HTML-only mails."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()

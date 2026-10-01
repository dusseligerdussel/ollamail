from app.mail.mime import parse_message
from app.mail.sanitize import html_to_text, sanitize_html
from tests.mail.conftest import load_fixture


def newsletter_html() -> str:
    html = parse_message(load_fixture("04-newsletter-html.eml")).html
    assert html is not None
    return html


def test_scripts_forms_frames_and_handlers_are_removed() -> None:
    safe = sanitize_html(newsletter_html()).html.lower()

    for forbidden in ("<script", "document.write", "<form", "<input", "<iframe", "onclick"):
        assert forbidden not in safe
    assert "javascript:" not in safe
    assert "<style" not in safe


def test_external_images_are_blocked_by_default() -> None:
    result = sanitize_html(newsletter_html())

    assert result.blocked_images == 2
    assert "tracker.example.net" not in result.html
    assert "cdn.example.net" not in result.html
    # The image element and its alt text remain, only the source is gone.
    assert 'alt="Banner"' in result.html


def test_external_images_can_be_allowed() -> None:
    result = sanitize_html(newsletter_html(), allow_external_images=True)

    assert result.blocked_images == 0
    assert 'src="https://cdn.example.net/banner.jpg"' in result.html


def test_css_cannot_load_resources_or_overlay() -> None:
    result = sanitize_html(newsletter_html(), allow_external_images=True)

    assert "url(" not in result.html
    assert "position" not in result.html
    assert "color:#333" in result.html


def test_links_open_safely() -> None:
    safe = sanitize_html('<a href="https://example.com/x">x</a>').html

    assert 'target="_blank"' in safe
    assert 'rel="noopener noreferrer nofollow"' in safe


def test_cid_images_are_resolved_or_dropped() -> None:
    html = '<img src="cid:logo@example.com"><img src="cid:unknown@example.com">'
    resolved = sanitize_html(
        html,
        resolve_cid=lambda cid: "/api/attachments/1" if cid == "logo@example.com" else None,
    )

    assert resolved.html.count('src="/api/attachments/1"') == 1
    assert "cid:" not in resolved.html
    assert resolved.blocked_images == 0
    assert "cid:" not in sanitize_html(html).html


def test_data_and_relative_image_sources_are_dropped() -> None:
    safe = sanitize_html('<img src="data:image/png;base64,AAAA"><img src="/local.png">').html

    assert "src=" not in safe


def test_protocol_relative_images_count_as_external() -> None:
    result = sanitize_html('<img src="//tracker.example.net/p.gif">')

    assert result.blocked_images == 1
    assert "tracker" not in result.html


def test_html_to_text_quotes_blockquotes() -> None:
    html = parse_message(load_fixture("03-gmail-reply-utf8.eml")).html
    assert html is not None
    text = html_to_text(html)

    assert text.startswith("Hi Max,")
    assert "> wer hat Zeit für einen Workshop am Donnerstag?" in text.splitlines()


def test_html_to_text_skips_invisible_content_and_keeps_structure() -> None:
    text = html_to_text(
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<p>First&nbsp;paragraph</p><ul><li>one</li><li>two</li></ul>"
        "<pre>  keep\n  spacing</pre><script>x()</script></body></html>"
    )

    assert text == "First paragraph\n- one\n- two\n  keep\n  spacing"

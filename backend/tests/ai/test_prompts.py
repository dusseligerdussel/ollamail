import pytest

from app.ai.prompts import PromptRegistry, PromptTemplate, registry


def _template(version: int = 1) -> PromptTemplate:
    return PromptTemplate(
        name="greet",
        version=version,
        system={"en": "Answer in $lang.", "de": "Antworte auf $lang."},
        user={"en": "Mail:\n$mail"},
    )


def test_render_uses_mail_language_with_fallback() -> None:
    template = _template()

    de = template.render("de-AT", lang="Deutsch", mail="x")
    fr = template.render("fr", lang="French", mail="x")
    unknown = template.render(None, lang="English", mail="x")

    assert de[0].content == "Antworte auf Deutsch."
    assert de[1].content == "Mail:\nx"  # user text falls back to English
    assert fr[0].content == "Answer in French."
    assert unknown[0].content == "Answer in English."


def test_values_are_inserted_verbatim() -> None:
    messages = _template().render("en", lang="en", mail="costs $amount {braces} ${x}")

    assert messages[1].content == "Mail:\ncosts $amount {braces} ${x}"


def test_english_text_is_required() -> None:
    with pytest.raises(ValueError):
        PromptTemplate(name="x", version=1, system={"de": "nur deutsch"})


def test_registry_returns_latest_or_requested_version() -> None:
    reg = PromptRegistry()
    reg.register(_template(1))
    reg.register(_template(2))

    assert reg.get("greet").version == 2
    assert reg.get("greet", 1).id == "greet@1"
    with pytest.raises(ValueError):
        reg.register(_template(2))
    with pytest.raises(KeyError):
        reg.get("missing")
    with pytest.raises(KeyError):
        reg.get("greet", 9)


def test_builtin_structured_output_prompts_are_registered() -> None:
    import app.ai.prompts.structured_output  # noqa: F401

    assert registry.get("structured_output").version >= 1
    assert registry.get("structured_output_retry").version >= 1

"""Versioned, language-aware prompt templates.

A template is immutable once released: change wording by registering a new version, so
stored results and metrics stay attributable to the exact prompt (``name@version``).
Placeholders use ``string.Template`` syntax (``$name``); substituted values are inserted
verbatim and never interpreted, so mail content containing ``$`` or braces is safe.
"""

from dataclasses import dataclass
from string import Template

from app.ai.llm.types import ChatMessage

DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: int
    # Texts per language code ("de", "en"); "en" is required as fallback.
    system: dict[str, str]
    user: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if DEFAULT_LANGUAGE not in self.system:
            raise ValueError(f"prompt {self.name!r} needs an {DEFAULT_LANGUAGE!r} system text")
        if self.user is not None and DEFAULT_LANGUAGE not in self.user:
            raise ValueError(f"prompt {self.name!r} needs an {DEFAULT_LANGUAGE!r} user text")

    @property
    def id(self) -> str:
        """Identifier stored with metrics and results, e.g. ``triage@2``."""
        return f"{self.name}@{self.version}"

    def language_for(self, language: str | None) -> str:
        """Best supported language for a mail language such as ``de``, ``de-AT`` or ``fr``."""
        if language:
            base = language.lower().replace("_", "-").split("-")[0]
            if base in self.system:
                return base
        return DEFAULT_LANGUAGE

    def render(self, language: str | None = None, **values: str) -> list[ChatMessage]:
        lang = self.language_for(language)
        system = Template(self.system[lang]).substitute(values)
        messages = [ChatMessage(role="system", content=system)]
        if self.user is not None:
            text = self.user.get(lang, self.user[DEFAULT_LANGUAGE])
            messages.append(ChatMessage(role="user", content=Template(text).substitute(values)))
        return messages


class PromptRegistry:
    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], PromptTemplate] = {}

    def register(self, template: PromptTemplate) -> PromptTemplate:
        key = (template.name, template.version)
        if key in self._templates:
            raise ValueError(f"prompt {template.id!r} is already registered")
        self._templates[key] = template
        return template

    def get(self, name: str, version: int | None = None) -> PromptTemplate:
        """A specific version, or the latest one."""
        if version is not None:
            try:
                return self._templates[(name, version)]
            except KeyError:
                raise KeyError(f"unknown prompt {name}@{version}") from None
        versions = [v for (n, v) in self._templates if n == name]
        if not versions:
            raise KeyError(f"unknown prompt {name!r}")
        return self._templates[(name, max(versions))]


registry = PromptRegistry()

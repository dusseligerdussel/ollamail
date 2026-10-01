"""Versioned prompt templates. Feature modules register theirs in this package."""

from app.ai.prompts.base import PromptRegistry, PromptTemplate, registry

__all__ = ["PromptRegistry", "PromptTemplate", "registry"]

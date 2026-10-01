"""Context-length management without a model-specific tokenizer.

The estimate is deliberately conservative (German and mail markup tokenize worse than
English prose), so prompts stay below the window across models.
"""

import math
from collections.abc import Sequence

from app.ai.llm.types import ChatMessage

CHARS_PER_TOKEN = 3.0
# Per-message overhead of chat templates (role markers, separators).
MESSAGE_OVERHEAD_TOKENS = 4
TRUNCATION_MARKER = "\n[…]"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: Sequence[ChatMessage]) -> int:
    return sum(estimate_tokens(m.content) + MESSAGE_OVERHEAD_TOKENS for m in messages)


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Shorten ``text`` to about ``max_tokens``, keeping the beginning (where mails carry
    the new content; quoted history follows below)."""
    if estimate_tokens(text) <= max_tokens:
        return text
    keep = max(0, int(max_tokens * CHARS_PER_TOKEN) - len(TRUNCATION_MARKER))
    return text[:keep].rstrip() + TRUNCATION_MARKER


def fit_messages(
    messages: Sequence[ChatMessage], *, context_tokens: int, reserve_tokens: int
) -> list[ChatMessage]:
    """Return messages that fit into ``context_tokens`` minus ``reserve_tokens`` (room for
    the answer) by shortening the longest non-system messages. System messages (instructions
    and output schema) are never shortened."""
    budget = context_tokens - reserve_tokens
    result = list(messages)
    excess = estimate_messages_tokens(result) - budget
    while excess > 0:
        candidates = [
            (estimate_tokens(m.content), i)
            for i, m in enumerate(result)
            if m.role != "system" and m.content
        ]
        if not candidates:
            break
        size, index = max(candidates)
        target = max(0, size - excess)
        shortened = truncate_to_tokens(result[index].content, target) if target else ""
        if shortened == result[index].content:
            break
        result[index] = result[index].model_copy(update={"content": shortened})
        excess = estimate_messages_tokens(result) - budget
    return result

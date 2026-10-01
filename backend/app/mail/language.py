"""Language detection for mail bodies (offline, ``py3langid`` with its bundled model)."""

import re
from functools import cache

from py3langid.langid import MODEL_FILE, LanguageIdentifier

# Below this many letters the result is mostly guesswork.
MIN_LETTERS = 20
MIN_PROBABILITY = 0.6
# Only the beginning matters; long bodies would just cost time.
MAX_CHARS = 4000

_URL = re.compile(r"\S+://\S+|\S+@\S+\.\S+|www\.\S+")


@cache
def _identifier() -> LanguageIdentifier:
    return LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def detect_language(text: str) -> str | None:
    """ISO 639-1 code (``de``, ``en``, ...) or ``None`` if the text is too short or the
    result is uncertain."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    sample = _URL.sub(" ", "\n".join(lines))[:MAX_CHARS]
    if sum(char.isalpha() for char in sample) < MIN_LETTERS:
        return None
    language, probability = _identifier().classify(sample)
    return str(language) if probability >= MIN_PROBABILITY else None

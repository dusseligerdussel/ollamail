"""Recovery codes: ten single-use codes, shown once, stored as keyed hashes.

A code has 10 characters from a 31-letter alphabet without look-alikes (~49 bits). With
that entropy plus the login rate limits, HMAC-SHA256 under a key derived from
``OLLAMAIL_SECRET_KEY`` is enough: a database dump alone does not reveal any code.
"""

import hmac
import secrets
from hashlib import sha256

from app.auth.keys import derive_key
from app.core.config import SecuritySettings

COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_LENGTH = 10


def generate() -> list[str]:
    """New codes, formatted ``xxxxx-xxxxx``."""
    codes = []
    for _ in range(COUNT):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize(code: str) -> str:
    return "".join(c for c in code.lower() if c.isalnum())


def looks_like_code(code: str) -> bool:
    normalized = normalize(code)
    return len(normalized) == _LENGTH and all(c in _ALPHABET for c in normalized)


def code_hash(settings: SecuritySettings, code: str) -> bytes:
    key = derive_key(settings, "mfa-recovery-code")
    return hmac.new(key, normalize(code).encode(), sha256).digest()

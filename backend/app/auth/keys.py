"""Purpose-bound keys derived from ``OLLAMAIL_SECRET_KEY`` (HKDF-SHA256).

All API instances derive the same keys, so CSRF tokens, rate-limit keys and the setup
token work across instances without shared state. Without a master key (only possible
in tests: the API refuses to start without one) a random per-process key is used.
"""

import hashlib
import hmac
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import SecuritySettings
from app.core.crypto import decode_key

_FALLBACK_MASTER = os.urandom(32)


@lru_cache(maxsize=32)
def _derive(master: bytes, purpose: str) -> bytes:
    info = f"ollamail auth {purpose}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


def derive_key(settings: SecuritySettings, purpose: str) -> bytes:
    master = (
        decode_key(settings.secret_key.get_secret_value())
        if settings.secret_key is not None
        else _FALLBACK_MASTER
    )
    return _derive(master, purpose)


def keyed_digest(key: bytes, value: str) -> str:
    """Hex HMAC-SHA256 of ``value``; stores lookups without the value itself."""
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()

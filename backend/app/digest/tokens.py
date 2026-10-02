"""Podcast feed tokens.

The token is the only credential of the feed and its audio URLs (podcast apps cannot
log in). It is 256 random bits, shown to the user once and stored only as SHA-256 hash,
so a database dump does not reveal working feed URLs. Lookup is by hash (unique index);
a token that is revoked or replaced has no row any more and yields 404.
"""

import hashlib
import re
import secrets

TOKEN_BYTES = 32
# ``secrets.token_urlsafe(32)`` yields 43 characters of the URL-safe base64 alphabet.
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def is_well_formed(token: str) -> bool:
    """Cheap check before a database lookup."""
    return TOKEN_PATTERN.match(token) is not None

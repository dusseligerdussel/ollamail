"""Password hashing with Argon2id.

Parameters follow the RFC 9106 low-memory profile (argon2-cffi default: t=3, 64 MiB, p=4).
Hashing runs in a worker thread so it does not block the event loop; a semaphore caps the
number of parallel hashes and therefore the memory a burst of logins can take.
Stored hashes carry their parameters, so changing them only affects new hashes;
``needs_rehash`` tells the login to upgrade an old hash.
"""

import asyncio
import threading

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher(type=Type.ID)
_slots = threading.BoundedSemaphore(4)
# Verified when the account does not exist, so the response time does not reveal it.
_dummy_hash: str | None = None


def configure_hasher(hasher: PasswordHasher) -> None:
    """Replace the hasher (tests use cheap parameters)."""
    global _hasher, _dummy_hash
    _hasher = hasher
    _dummy_hash = None


def _hash(password: str) -> str:
    with _slots:
        return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    with _slots:
        try:
            return _hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hash, password)


async def verify_password(password_hash: str | None, password: str) -> bool:
    """Check ``password``; with ``password_hash=None`` a dummy hash is verified and the
    result is always ``False`` (constant work for unknown accounts)."""
    global _dummy_hash
    if password_hash is None:
        if _dummy_hash is None:
            _dummy_hash = await hash_password("ollamail-dummy-password")
        await asyncio.to_thread(_verify, _dummy_hash, password)
        return False
    return await asyncio.to_thread(_verify, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True

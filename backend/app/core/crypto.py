"""Envelope encryption for stored secrets (mailbox passwords, OAuth tokens, IdP secrets).

Every secret gets its own random 256-bit data key (DEK). The value is encrypted with the
DEK using AES-256-GCM; the DEK itself is encrypted ("wrapped") with AES-256-GCM under a
key-encryption key (KEK) derived from the master key ``OLLAMAIL_SECRET_KEY``.

Token layout (stored as unpadded URL-safe base64 text)::

    version (1) | key_id (4) | dek_nonce (12) | wrapped_dek (32 + 16 tag)
                | data_nonce (12) | ciphertext + tag (16)

``version`` and ``key_id`` are authenticated as associated data when wrapping the DEK;
``version`` also when encrypting the value (the value is bound to its DEK anyway).
``key_id`` identifies the master key, so several master keys can be valid at once
(``OLLAMAIL_SECRET_KEYS_OLD``). Rotating only re-wraps the DEK; the encrypted value is
left untouched (see ``KeyRing.rewrap`` and ``rotate_keys``).

Models use the column types ``EncryptedStr`` and ``EncryptedJSON``; they encrypt with the
process-wide key ring (``get_keyring``). Neither keys nor plaintexts ever appear in
exception messages or logs.
"""

import base64
import binascii
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import MetaData, Text, select, type_coerce, update
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import TypeDecorator

from app.core.config import SecuritySettings, get_settings
from app.core.logging import get_logger

VERSION = 1
MIN_KEY_BYTES = 32
# Fewer distinct byte values than this in a 32-byte key means it was not randomly generated.
_MIN_DISTINCT_BYTES = 16

_KEY_ID_LEN = 4
_NONCE_LEN = 12
_DEK_LEN = 32
_TAG_LEN = 16
_WRAPPED_DEK_LEN = _DEK_LEN + _TAG_LEN
_HEADER_LEN = 1 + _KEY_ID_LEN
_DATA_OFFSET = _HEADER_LEN + _NONCE_LEN + _WRAPPED_DEK_LEN
_MIN_TOKEN_LEN = _DATA_OFFSET + _NONCE_LEN + _TAG_LEN

_KEK_INFO = b"ollamail/secret-kek/v1"
_KEY_ID_INFO = b"ollamail/secret-key-id/v1"


class CryptoError(Exception):
    """Base class; messages never contain key material or plaintext."""


class SecretKeyError(CryptoError):
    """``OLLAMAIL_SECRET_KEY`` (or an old key) is missing or too weak."""


class DecryptionError(CryptoError):
    """A token is malformed, was tampered with or uses an unknown master key."""


def generate_key() -> str:
    """Return a new random master key in the format ``OLLAMAIL_SECRET_KEY`` expects."""
    return base64.b64encode(secrets.token_bytes(MIN_KEY_BYTES)).decode("ascii")


def decode_key(value: str, *, name: str = "OLLAMAIL_SECRET_KEY") -> bytes:
    """Decode and check a base64 master key (standard or URL-safe alphabet)."""
    value = value.strip()
    if not value:
        raise SecretKeyError(f"{name} is not set")
    try:
        raw = base64.b64decode(value.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError):
        raise SecretKeyError(f"{name} is not valid base64") from None
    if len(raw) < MIN_KEY_BYTES:
        raise SecretKeyError(f"{name} must decode to at least {MIN_KEY_BYTES} bytes")
    if len(set(raw)) < _MIN_DISTINCT_BYTES:
        raise SecretKeyError(f"{name} is too weak; generate a random key")
    return raw


def _derive(master: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info).derive(master)


@dataclass(frozen=True, repr=False)
class _MasterKey:
    key_id: bytes
    kek: AESGCM

    def __repr__(self) -> str:
        return f"_MasterKey(key_id={self.key_id.hex()})"


def _master_key(raw: bytes) -> _MasterKey:
    return _MasterKey(
        key_id=_derive(raw, _KEY_ID_INFO, _KEY_ID_LEN),
        kek=AESGCM(_derive(raw, _KEK_INFO, 32)),
    )


def _data_aad(header: bytes) -> bytes:
    return header[:1]


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(token: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (binascii.Error, ValueError):
        raise DecryptionError("token is not valid base64") from None


@dataclass(frozen=True)
class _Envelope:
    header: bytes
    dek_nonce: bytes
    wrapped_dek: bytes
    data: bytes

    @property
    def key_id(self) -> bytes:
        return self.header[1:]

    @classmethod
    def parse(cls, token: str) -> "_Envelope":
        raw = _b64decode(token)
        if len(raw) < _MIN_TOKEN_LEN:
            raise DecryptionError("token is too short")
        if raw[0] != VERSION:
            raise DecryptionError("unsupported token version")
        offset = _HEADER_LEN
        return cls(
            header=raw[:offset],
            dek_nonce=raw[offset : offset + _NONCE_LEN],
            wrapped_dek=raw[offset + _NONCE_LEN : _DATA_OFFSET],
            data=raw[_DATA_OFFSET:],
        )

    def encode(self) -> str:
        return _b64encode(self.header + self.dek_nonce + self.wrapped_dek + self.data)


class KeyRing:
    """The current master key (encrypts) plus old master keys (decrypt only)."""

    def __init__(self, current: bytes, old: Iterable[bytes] = ()) -> None:
        self._current = _master_key(current)
        self._keys: dict[bytes, _MasterKey] = {self._current.key_id: self._current}
        for raw in old:
            key = _master_key(raw)
            self._keys.setdefault(key.key_id, key)

    @classmethod
    def from_settings(cls, settings: SecuritySettings) -> "KeyRing":
        """Build the key ring; raises ``SecretKeyError`` if a key is missing or weak."""
        current = settings.secret_key.get_secret_value() if settings.secret_key else ""
        old = [
            decode_key(key.get_secret_value(), name=f"OLLAMAIL_SECRET_KEYS_OLD[{index}]")
            for index, key in enumerate(settings.secret_keys_old)
        ]
        return cls(decode_key(current), old)

    @property
    def current_key_id(self) -> str:
        return self._current.key_id.hex()

    def __repr__(self) -> str:
        return f"KeyRing(current={self.current_key_id}, keys={len(self._keys)})"

    def encrypt(self, plaintext: bytes) -> str:
        header = bytes([VERSION]) + self._current.key_id
        dek = AESGCM.generate_key(bit_length=256)
        dek_nonce = os.urandom(_NONCE_LEN)
        data_nonce = os.urandom(_NONCE_LEN)
        envelope = _Envelope(
            header=header,
            dek_nonce=dek_nonce,
            wrapped_dek=self._current.kek.encrypt(dek_nonce, dek, header),
            data=data_nonce + AESGCM(dek).encrypt(data_nonce, plaintext, _data_aad(header)),
        )
        return envelope.encode()

    def decrypt(self, token: str) -> bytes:
        envelope = _Envelope.parse(token)
        dek = self._unwrap(envelope)
        nonce, ciphertext = envelope.data[:_NONCE_LEN], envelope.data[_NONCE_LEN:]
        try:
            return AESGCM(dek).decrypt(nonce, ciphertext, _data_aad(envelope.header))
        except InvalidTag:
            raise DecryptionError("authentication failed") from None

    def key_id(self, token: str) -> str:
        """Hex ID of the master key a token was encrypted with."""
        return _Envelope.parse(token).key_id.hex()

    def needs_rotation(self, token: str) -> bool:
        return _Envelope.parse(token).key_id != self._current.key_id

    def rewrap(self, token: str) -> str:
        """Re-encrypt the token's data key with the current master key.

        The encrypted value is kept as is; only the DEK wrapping and the key ID change.
        Raises ``DecryptionError`` if the data key cannot be unwrapped.
        """
        envelope = _Envelope.parse(token)
        dek = self._unwrap(envelope)
        header = bytes([VERSION]) + self._current.key_id
        dek_nonce = os.urandom(_NONCE_LEN)
        rewrapped = _Envelope(
            header=header,
            dek_nonce=dek_nonce,
            wrapped_dek=self._current.kek.encrypt(dek_nonce, dek, header),
            data=envelope.data,
        )
        return rewrapped.encode()

    def _unwrap(self, envelope: _Envelope) -> bytes:
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise DecryptionError("token was encrypted with an unknown master key")
        try:
            return key.kek.decrypt(envelope.dek_nonce, envelope.wrapped_dek, envelope.header)
        except InvalidTag:
            raise DecryptionError("authentication failed") from None


log = get_logger(__name__)

_keyring: KeyRing | None = None


def get_keyring() -> KeyRing:
    """Process-wide key ring, built from the settings on first use."""
    global _keyring
    if _keyring is None:
        _keyring = KeyRing.from_settings(get_settings().security)
    return _keyring


def set_keyring(keyring: KeyRing | None) -> None:
    """Replace the process-wide key ring (``None`` rebuilds it from the settings)."""
    global _keyring
    _keyring = keyring


def configure_keyring(settings: SecuritySettings) -> KeyRing:
    """Build the key ring at startup and install it process-wide.

    Raises ``SecretKeyError`` if a key is missing or too weak. The reason is also logged,
    because the logging pipeline drops exception messages.
    """
    try:
        keyring = KeyRing.from_settings(settings)
    except SecretKeyError as exc:
        # The message only names the setting, never the key itself.
        log.error("secret_key_invalid", reason=str(exc))
        raise
    set_keyring(keyring)
    log.info("keyring_configured", key_id=keyring.current_key_id)
    return keyring


class _EncryptedMixin:
    """Marker for columns that ``rotate_keys`` re-wraps."""


class EncryptedStr(_EncryptedMixin, TypeDecorator[str]):
    """A string column stored as an encrypted token (``TEXT``)."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_keyring().encrypt(value.encode("utf-8"))

    def process_result_value(self, value: Any | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_keyring().decrypt(value).decode("utf-8")


class EncryptedJSON(_EncryptedMixin, TypeDecorator[Any]):
    """A JSON-serialisable value stored as an encrypted token (``TEXT``).

    SQL ``NULL`` stands for ``None``; values are not queryable in the database.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        return get_keyring().encrypt(payload.encode("utf-8"))

    def process_result_value(self, value: Any | None, dialect: Dialect) -> Any | None:
        if value is None:
            return None
        return json.loads(get_keyring().decrypt(value))


@dataclass
class RotationResult:
    tables: int = 0
    checked: int = 0
    rotated: int = 0


async def rotate_keys(
    connection: AsyncConnection, keyring: KeyRing, metadata: MetaData
) -> RotationResult:
    """Re-wrap every encrypted column value in ``metadata`` with the current master key.

    Runs in the caller's transaction; the caller commits. Raises ``DecryptionError`` if
    a value uses a master key that is not in the key ring, so nothing is half-rotated
    when the transaction is rolled back. Secret tables are small, so each table is read
    at once.
    """
    result = RotationResult()
    for table in metadata.sorted_tables:
        columns = [c for c in table.columns if isinstance(c.type, _EncryptedMixin)]
        if not columns:
            continue
        primary_key = list(table.primary_key.columns)
        if not primary_key:
            raise CryptoError(f"table {table.name} has encrypted columns but no primary key")
        result.tables += 1
        raw = [type_coerce(c, Text).label(c.name) for c in columns]
        rows = (await connection.execute(select(*primary_key, *raw))).all()
        for row in rows:
            values: dict[str, Any] = {}
            for column in columns:
                token = row._mapping[column.name]
                if token is None:
                    continue
                result.checked += 1
                if keyring.needs_rotation(token):
                    values[column.name] = type_coerce(keyring.rewrap(token), Text)
            if not values:
                continue
            condition = [pk == row._mapping[pk] for pk in primary_key]
            await connection.execute(update(table).where(*condition).values(values))
            result.rotated += len(values)
    return result

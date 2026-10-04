"""VAPID keys and Web Push message encryption, built on ``cryptography`` only.

* VAPID (RFC 8292): the server signs a short-lived JWT (ES256) per push service origin, so
  the push service accepts messages for subscriptions created with our public key.
* Message encryption (RFC 8291, ``aes128gcm`` from RFC 8188): only the browser that holds
  the subscription's private key can read the payload; the push service cannot.

Keys are URL-safe base64 without padding, the format browsers and the usual tools use:
the public key as uncompressed P-256 point (65 bytes), the private key as raw scalar
(32 bytes). Errors never contain key material.
"""

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PUBLIC_KEY_LEN = 65
PRIVATE_KEY_LEN = 32
AUTH_SECRET_LEN = 16
SALT_LEN = 16
# One record holds the whole (small) payload.
RECORD_SIZE = 4096
# Push services accept tokens valid for at most 24 hours.
JWT_LIFETIME_SECONDS = 12 * 60 * 60

_CURVE = ec.SECP256R1()


class VapidKeyError(ValueError):
    """A key or subscription key is malformed (message without key material)."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    value = value.strip()
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise VapidKeyError("not URL-safe base64") from None


def public_key_from_bytes(data: bytes) -> ec.EllipticCurvePublicKey:
    if len(data) != PUBLIC_KEY_LEN or data[0] != 0x04:
        raise VapidKeyError("not an uncompressed P-256 public key")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(_CURVE, data)
    except ValueError:
        raise VapidKeyError("not a point on P-256") from None


def public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def private_key_from_raw(value: str) -> ec.EllipticCurvePrivateKey:
    data = b64url_decode(value)
    if len(data) != PRIVATE_KEY_LEN:
        raise VapidKeyError("the private key must be 32 bytes")
    try:
        return ec.derive_private_key(int.from_bytes(data, "big"), _CURVE)
    except ValueError:
        raise VapidKeyError("not a valid P-256 private key") from None


def generate_key_pair() -> tuple[str, str]:
    """A new VAPID key pair as ``(public, private)``."""
    key = ec.generate_private_key(_CURVE)
    private = key.private_numbers().private_value.to_bytes(PRIVATE_KEY_LEN, "big")
    return b64url_encode(public_key_bytes(key.public_key())), b64url_encode(private)


def check_key_pair(public: str, private: str) -> None:
    """Raise ``VapidKeyError`` unless both keys are well-formed and belong together."""
    public_bytes = b64url_decode(public)
    public_key_from_bytes(public_bytes)
    derived = public_key_bytes(private_key_from_raw(private).public_key())
    if derived != public_bytes:
        raise VapidKeyError("the VAPID public key does not belong to the private key")


def check_subscription_keys(p256dh: str, auth: str) -> None:
    """Raise ``VapidKeyError`` unless the browser's keys have the expected form."""
    public_key_from_bytes(b64url_decode(p256dh))
    if len(b64url_decode(auth)) != AUTH_SECRET_LEN:
        raise VapidKeyError("the auth secret must be 16 bytes")


@dataclass(frozen=True)
class Vapid:
    """The server's VAPID identity."""

    private_key: ec.EllipticCurvePrivateKey
    public_key: str
    subject: str

    @classmethod
    def from_keys(cls, public: str, private: str, subject: str) -> "Vapid":
        check_key_pair(public, private)
        return cls(private_key_from_raw(private), public.strip(), subject)

    def authorization(self, audience: str, *, now: float | None = None) -> str:
        """``Authorization`` header for a push service origin (``https://host``)."""
        header = {"typ": "JWT", "alg": "ES256"}
        claims = {
            "aud": audience,
            "exp": int((now if now is not None else time.time()) + JWT_LIFETIME_SECONDS),
            "sub": self.subject,
        }
        signing_input = ".".join(
            b64url_encode(json.dumps(part, separators=(",", ":")).encode("ascii"))
            for part in (header, claims)
        ).encode("ascii")
        der = self.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        token = f"{signing_input.decode('ascii')}.{b64url_encode(signature)}"
        return f"vapid t={token}, k={self.public_key}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    # Extract and expand in one go, as RFC 8291 and RFC 8188 use it.
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(
    payload: bytes,
    p256dh: str,
    auth: str,
    *,
    sender_key: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Encrypt ``payload`` for one subscription (RFC 8291). ``sender_key`` and ``salt``
    are random per message; tests pass fixed ones to compare with the RFC's example."""
    ua_public_bytes = b64url_decode(p256dh)
    ua_public = public_key_from_bytes(ua_public_bytes)
    auth_secret = b64url_decode(auth)
    if len(auth_secret) != AUTH_SECRET_LEN:
        raise VapidKeyError("the auth secret must be 16 bytes")
    if len(payload) + 1 + 16 > RECORD_SIZE - 86:
        raise ValueError("payload too large for one push message")

    sender_key = sender_key or ec.generate_private_key(_CURVE)
    salt = salt if salt is not None else os.urandom(SALT_LEN)
    as_public_bytes = public_key_bytes(sender_key.public_key())

    shared = sender_key.exchange(ec.ECDH(), ua_public)
    key_info = b"WebPush: info\x00" + ua_public_bytes + as_public_bytes
    ikm = _hkdf(auth_secret, shared, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02: delimiter of the last (and only) record, no further padding.
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = (
        salt
        + RECORD_SIZE.to_bytes(4, "big")
        + len(as_public_bytes).to_bytes(1, "big")
        + as_public_bytes
    )
    return header + ciphertext

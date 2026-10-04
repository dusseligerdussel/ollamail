"""A fake browser for Web Push tests: its subscription keys and RFC 8291 decryption, and the
sign-in session a device is registered in."""

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import AuthSession
from app.core.config import NotificationsSettings
from app.notifications.vapid import (
    _hkdf,
    b64url_encode,
    generate_key_pair,
    public_key_bytes,
    public_key_from_bytes,
)

FCM = "https://fcm.googleapis.com/fcm/send/"
MOZILLA = "https://updates.push.services.mozilla.com/wpush/v2/"


def push_settings(**overrides: object) -> NotificationsSettings:
    """Web Push switched on with a fresh key pair."""
    public, private = generate_key_pair()
    values: dict[str, object] = {
        "web_push_enabled": True,
        "vapid_public_key": public,
        "vapid_private_key": private,
        "vapid_subject": "mailto:admin@example.org",
        **overrides,
    }
    return NotificationsSettings.model_validate(values)


@dataclass
class Browser:
    """Keys of one browser's push subscription."""

    endpoint: str
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )
    auth_secret: bytes = b"0123456789abcdef"

    @property
    def p256dh(self) -> str:
        return b64url_encode(public_key_bytes(self.key.public_key()))

    @property
    def auth(self) -> str:
        return b64url_encode(self.auth_secret)

    def subscription(self) -> dict[str, object]:
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}

    def decrypt(self, body: bytes) -> bytes:
        """What the browser reads from an ``aes128gcm`` push message (single record)."""
        salt, key_length = body[:16], body[20]
        sender_public = body[21 : 21 + key_length]
        ciphertext = body[21 + key_length :]
        shared = self.key.exchange(ec.ECDH(), public_key_from_bytes(sender_public))
        ua_public = public_key_bytes(self.key.public_key())
        info = b"WebPush: info\x00" + ua_public + sender_public
        ikm = _hkdf(self.auth_secret, shared, info, 32)
        cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
        assert plain.endswith(b"\x02")
        return plain[:-1]

    def payload(self, body: bytes) -> object:
        return json.loads(self.decrypt(body))


async def make_auth_session(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
    last_seen_at: datetime | None = None,
) -> uuid.UUID:
    """A sign-in session of the user (devices are bound to one, #185)."""
    now = datetime.now(UTC)
    auth_session = AuthSession(
        user_id=user_id,
        token_hash=os.urandom(32),
        provider="local",
        expires_at=expires_at or now + timedelta(days=1),
        last_seen_at=last_seen_at or now,
        authenticated_at=now,
    )
    session.add(auth_session)
    await session.flush()
    return auth_session.id

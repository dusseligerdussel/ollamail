"""``clientState`` of Graph change notifications (docs/providers/microsoft365.md §5.4).

The value is ``<mailbox_id>.<HMAC-SHA256(mailbox_id)>`` with a key derived from
``OLLAMAIL_SECRET_KEY``. The notification endpoint only trusts notifications whose
``clientState`` verifies, and takes the mailbox ID from it; nothing else of a notification
is used.
"""

import base64
import hashlib
import hmac
import uuid

from app.auth.keys import derive_key
from app.core.config import SecuritySettings

_PURPOSE = "graph-notifications"
_MAC_BYTES = 24


def _mac(security: SecuritySettings, mailbox_id: uuid.UUID) -> str:
    key = derive_key(security, _PURPOSE)
    digest = hmac.new(key, str(mailbox_id).encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_MAC_BYTES]).rstrip(b"=").decode("ascii")


def client_state(security: SecuritySettings, mailbox_id: uuid.UUID) -> str:
    """At most 128 characters, as Graph requires (36 + 1 + 32)."""
    return f"{mailbox_id}.{_mac(security, mailbox_id)}"


def verify_client_state(security: SecuritySettings, value: object) -> uuid.UUID | None:
    """Mailbox ID of a valid ``clientState``, else ``None``."""
    if not isinstance(value, str) or value.count(".") != 1:
        return None
    raw_id, mac = value.split(".")
    try:
        mailbox_id = uuid.UUID(raw_id)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, _mac(security, mailbox_id)):
        return None
    return mailbox_id

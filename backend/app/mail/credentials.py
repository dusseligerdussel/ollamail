"""Column type for encrypted mailbox credentials.

TODO(#6): Replace ``PendingEncryptedJSON`` with the ``EncryptedJSON`` type from
``app.core`` once #6 (secret encryption) is merged. Until then this placeholder refuses
to store anything, so credentials can never end up in the database unencrypted.

The placeholder already uses the storage format #6 is expected to use (``bytea`` holding
the envelope-encrypted JSON document); if #6 settles on a different underlying type, the
swap needs a small follow-up migration for ``mail_mailboxes.credentials``.
"""

from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class CredentialsEncryptionUnavailableError(RuntimeError):
    """Raised when credentials are written before encryption (#6) is available."""


class PendingEncryptedJSON(TypeDecorator[dict[str, Any]]):
    """Placeholder for ``EncryptedJSON`` (#6): stores ``NULL`` only, never plaintext."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        raise CredentialsEncryptionUnavailableError(
            "mailbox credentials cannot be stored before secret encryption (#6) exists"
        )

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> dict[str, Any] | None:
        if value is None:
            return None
        raise CredentialsEncryptionUnavailableError(
            "mailbox credentials cannot be read before secret encryption (#6) exists"
        )

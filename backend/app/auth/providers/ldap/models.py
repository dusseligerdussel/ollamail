"""Configured LDAP / Active Directory directories."""

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedStr
from app.core.db import Base

PROVIDER_PREFIX = "ldap:"


class LdapDirectory(Base):
    """One directory; users sign in through it as provider ``ldap:<name>``.

    ``settings`` holds ``LdapDirectorySettings`` (no secrets); the service account's
    password is encrypted (``EncryptedStr``, docs/PRIVACY.md).
    """

    __tablename__ = "auth_ldap_directories"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    bind_password: Mapped[str] = mapped_column(EncryptedStr())

    @property
    def provider(self) -> str:
        return PROVIDER_PREFIX + self.name

"""OIDC providers configured in the admin API. The client secret is encrypted at rest."""

from sqlalchemy import Enum, String, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.providers.oidc.presets import OIDCPreset
from app.core.crypto import EncryptedStr
from app.core.db import Base


class OIDCProviderRecord(Base):
    """One OIDC provider; its identities use ``provider = 'oidc:<name>'``.

    Deleting a provider keeps the users and their identities: recreating it under the
    same name with the same IdP restores the logins.
    """

    __tablename__ = "auth_oidc_providers"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    preset: Mapped[OIDCPreset] = mapped_column(
        Enum(
            OIDCPreset,
            name="oidc_preset",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=OIDCPreset.GENERIC,
    )
    issuer: Mapped[str] = mapped_column(String(2048))
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret: Mapped[str | None] = mapped_column(EncryptedStr)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    enabled: Mapped[bool] = mapped_column(server_default=true(), default=True)
    auto_provision: Mapped[bool] = mapped_column(default=True)
    link_by_email: Mapped[bool] = mapped_column(default=False)
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    groups_claim: Mapped[str | None] = mapped_column(String(255))
    allowed_tenants: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    hosted_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)

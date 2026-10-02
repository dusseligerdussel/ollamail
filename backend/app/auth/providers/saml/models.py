"""SAML providers configured in the admin API and the assertion replay cache.

Nothing here is secret: the IdP certificates are public keys and ollamail signs nothing,
so the table holds no encrypted columns.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, LargeBinary, String, Table, Text, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SAMLProviderRecord(Base):
    """One SAML 2.0 IdP; identities use ``provider = 'saml:<name>'`` and the NameID (or the
    configured subject attribute) as subject.

    Deleting a provider keeps the users and their identities: recreating it under the
    same name restores the logins.
    """

    __tablename__ = "auth_saml_providers"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # Defaults and hints for the admin UI (entra, adfs, okta, keycloak, generic).
    preset: Mapped[str] = mapped_column(String(32))
    # Where the IdP metadata was loaded from (refreshable); null if uploaded or typed in.
    metadata_url: Mapped[str | None] = mapped_column(String(2048))
    metadata_refreshed_at: Mapped[datetime | None]
    idp_entity_id: Mapped[str] = mapped_column(String(1024))
    # Single sign-on endpoint (HTTP-Redirect binding).
    idp_sso_url: Mapped[str] = mapped_column(String(2048))
    # Signing certificates (base64 DER); several during a certificate rollover.
    idp_certificates: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # Entity ID of ollamail at this IdP; null for the SP metadata URL.
    sp_entity_id: Mapped[str | None] = mapped_column(String(1024))
    name_id_format: Mapped[str] = mapped_column(String(128))
    # Attribute with the stable user ID; null uses the NameID.
    subject_attribute: Mapped[str | None] = mapped_column(String(255))
    email_attribute: Mapped[str | None] = mapped_column(String(255))
    display_name_attribute: Mapped[str | None] = mapped_column(String(255))
    groups_attribute: Mapped[str | None] = mapped_column(String(255))
    # The IdP vouches for the e-mail address (directory-managed); needed for domain
    # allowlists and account linking.
    trust_email: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(server_default=true(), default=True)
    auto_provision: Mapped[bool] = mapped_column(default=True)
    link_by_email: Mapped[bool] = mapped_column(default=False)
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)


# Assertion IDs already used for a login (replay protection). ``key`` is the SHA-256 of
# provider and assertion ID; rows are deleted once ``expires_at`` has passed.
assertions = Table(
    "auth_saml_assertions",
    Base.metadata,
    Column("key", LargeBinary(32), primary_key=True),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
)

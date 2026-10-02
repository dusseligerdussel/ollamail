"""GitHub providers configured in the admin API. The client secret is encrypted at rest."""

from sqlalchemy import String, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedStr
from app.core.db import Base


class GitHubProviderRecord(Base):
    """One GitHub (or GitHub Enterprise Server) login; identities use
    ``provider = 'github:<name>'`` and the numeric GitHub user ID as subject.

    Deleting a provider keeps the users and their identities: recreating it under the
    same name for the same GitHub instance restores the logins.
    """

    __tablename__ = "auth_github_providers"

    name: Mapped[str] = mapped_column(String(32), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # GitHub Enterprise Server, e.g. https://github.example.org; null for github.com.
    base_url: Mapped[str | None] = mapped_column(String(2048))
    client_id: Mapped[str] = mapped_column(String(255))
    client_secret: Mapped[str] = mapped_column(EncryptedStr)
    enabled: Mapped[bool] = mapped_column(server_default=true(), default=True)
    auto_provision: Mapped[bool] = mapped_column(default=True)
    link_by_email: Mapped[bool] = mapped_column(default=False)
    allowed_domains: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)
    # Organization logins (lower case); members of any of them may sign in.
    allowed_organizations: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list)
    # Teams as "<org>/<team-slug>" (lower case); members of any of them may sign in.
    allowed_teams: Mapped[list[str]] = mapped_column(ARRAY(String(255)), default=list)

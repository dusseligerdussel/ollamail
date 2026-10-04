"""Users and roles.

One organisation per instance (docs/ARCHITECTURE.md §5). How a user signs in is stored
separately in ``app.auth.models.Identity``; a user can have several identities.
Deleting a user cascades to identities, sessions and owned mailboxes (docs/PRIVACY.md);
users with mailboxes are deleted in the background (``app.privacy.deletion``).
"""

import enum
from datetime import datetime

from sqlalchemy import Enum, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    # Stored normalised (trimmed, lower case), see ``app.users.service.normalize_email``.
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    # VARCHAR + CHECK instead of a native enum: new roles need no type migration.
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda members: [member.value for member in members],
        ),
        default=UserRole.USER,
    )
    # UI language (ISO 639-1) and IANA time zone.
    language: Mapped[str] = mapped_column(String(8), default="en")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # Inactive users cannot sign in; their sessions stop working immediately.
    is_active: Mapped[bool] = mapped_column(server_default=true(), default=True)
    last_login_at: Mapped[datetime | None]
    # Set while the user is being deleted in the background (``app.privacy.deletion``,
    # #177): the user is deactivated, anonymised and hidden everywhere until the row goes.
    deletion_requested_at: Mapped[datetime | None]

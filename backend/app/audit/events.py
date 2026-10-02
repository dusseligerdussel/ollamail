"""Audit event types, actors and targets.

Every security-relevant action has a fixed ``AuditAction``. Types marked "planned" are
defined here so the admin UI and the docs know them; the features that emit them do not
exist yet and call ``app.audit.record`` once they are built (see docs/PRIVACY.md).
"""

import enum
import uuid
from dataclasses import dataclass


class AuditAction(enum.StrEnum):
    # Authentication (app/auth)
    SETUP_COMPLETED = "auth.setup_completed"
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    SESSION_REVOKED = "auth.session_revoked"
    # Users
    USER_CREATED = "user.created"
    USER_ROLE_CHANGED = "user.role_changed"  # LDAP group sync; user administration (#33)
    USER_DELETED = "user.deleted"  # planned: user administration (#33)
    # Sign-in providers
    IDP_CONFIG_CHANGED = "idp.config_changed"  # LDAP (#32); planned: OIDC (#30)
    # AI settings, including enabling cloud providers
    AI_SETTINGS_CHANGED = "ai.settings_changed"  # planned: admin LLM settings
    # Mailboxes
    MAILBOX_CREATED = "mailbox.created"  # Microsoft 365 connect (#37); planned: mailbox API (#15)
    MAILBOX_DELETED = "mailbox.deleted"
    MAILBOX_SHARED = "mailbox.shared"  # planned: shared mailboxes
    # Data subject rights and data deletion
    DATA_EXPORTED = "data.exported"  # planned: personal data export
    DATA_DELETED = "data.deleted"  # planned: retention/deletion jobs (#36)
    # Instance operations
    KEYS_ROTATED = "crypto.keys_rotated"
    AUDIT_EXPORTED = "audit.exported"


class ActorKind(enum.StrEnum):
    USER = "user"
    # CLI commands and background jobs.
    SYSTEM = "system"
    # Not signed in, e.g. a failed login.
    ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Actor:
    """Who did it. Use the constructors: ``Actor.user(id)``, ``SYSTEM``, ``ANONYMOUS``."""

    kind: ActorKind
    user_id: uuid.UUID | None = None

    @classmethod
    def user(cls, user_id: uuid.UUID) -> "Actor":
        return cls(ActorKind.USER, user_id)


SYSTEM = Actor(ActorKind.SYSTEM)
ANONYMOUS = Actor(ActorKind.ANONYMOUS)


class TargetType(enum.StrEnum):
    USER = "user"
    SESSION = "session"
    MAILBOX = "mailbox"
    IDP = "idp"
    SETTINGS = "settings"


@dataclass(frozen=True)
class Target:
    """What it was done to: a type and an opaque ID (never a name or address)."""

    type: TargetType
    id: str | None = None

    @classmethod
    def of(cls, type_: TargetType, id_: uuid.UUID | str | None = None) -> "Target":
        return cls(type_, None if id_ is None else str(id_))

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
    # Second factor of local accounts (#96): passkey or TOTP added/removed (``method``),
    # recovery codes generated; removal by ``app.cli reset-password --reset-2fa`` (via cli).
    MFA_ENABLED = "auth.mfa_enabled"
    MFA_DISABLED = "auth.mfa_disabled"
    MFA_RECOVERY_CODES_GENERATED = "auth.mfa_recovery_codes_generated"
    # Confirmation before a sensitive action (#144): ``method``; failures with ``reason``.
    REAUTHENTICATED = "auth.reauthenticated"
    REAUTH_FAILED = "auth.reauth_failed"
    # Users
    USER_CREATED = "user.created"
    USER_ROLE_CHANGED = "user.role_changed"  # group mapping at login; user administration
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_INVITED = "user.invited"
    # Local password set: invitation accepted, CLI reset-password.
    USER_PASSWORD_SET = "user.password_set"
    USER_DELETED = "user.deleted"  # account deletion by the user or an admin (app/privacy)
    # Profile attributes changed by SCIM provisioning (#95): names of the fields only.
    USER_UPDATED = "user.updated"
    # Groups pushed by SCIM provisioning (#95) and their members.
    GROUP_CREATED = "group.created"
    GROUP_UPDATED = "group.updated"
    GROUP_DELETED = "group.deleted"
    GROUP_MEMBER_ADDED = "group.member_added"
    GROUP_MEMBER_REMOVED = "group.member_removed"
    # Sign-in providers
    # LDAP (#32), OIDC (#30), GitHub (#31), local login switch and group → role mapping (#33),
    # SCIM switch and tokens (#95)
    IDP_CONFIG_CHANGED = "idp.config_changed"
    # AI settings, including enabling cloud providers
    AI_SETTINGS_CHANGED = "ai.settings_changed"  # planned: admin LLM settings
    # Mailboxes
    MAILBOX_CREATED = "mailbox.created"
    MAILBOX_DELETED = "mailbox.deleted"
    # Shared mailboxes (#34): a user or group was given resp. lost access.
    MAILBOX_SHARED = "mailbox.shared"
    MAILBOX_UNSHARED = "mailbox.unshared"
    # A reply was sent from a mailbox (app/drafts): IDs and counts only.
    MAIL_SENT = "mail.sent"
    # A user archived, moved or trashed a mail (app/mail/actions, #148): IDs only.
    MAIL_MOVED = "mail.moved"
    # A user flagged or unflagged a mail (#148).
    MAIL_FLAGGED = "mail.flagged"
    # Data subject rights and data deletion
    DATA_EXPORTED = "data.exported"  # personal data export: requested, downloaded
    DATA_DELETED = "data.deleted"  # retention job (counts only)
    RETENTION_CHANGED = "data.retention_changed"  # admin retention settings
    # Todo export (#40) connected, changed or disconnected: todos go to a third party.
    TODO_EXPORT_CHANGED = "todo_export.changed"
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
    GROUP = "group"


@dataclass(frozen=True)
class Target:
    """What it was done to: a type and an opaque ID (never a name or address)."""

    type: TargetType
    id: str | None = None

    @classmethod
    def of(cls, type_: TargetType, id_: uuid.UUID | str | None = None) -> "Target":
        return cls(type_, None if id_ is None else str(id_))

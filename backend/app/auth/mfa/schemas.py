"""API schemas for second factors and the login steps after the password."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from app.users.schemas import UserRead

MfaMethod = Literal["webauthn", "totp", "recovery"]
PasskeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
# Browser output of navigator.credentials.create/get, serialised as JSON (base64url fields).
WebAuthnCredential = Annotated[dict[str, Any], Field(json_schema_extra={"type": "object"})]


class MfaChallenge(BaseModel):
    """Answer of ``POST /auth/login`` (202) when the password was right but the login
    needs a second step. No session exists yet; the step is bound to a short-lived cookie.
    """

    # ``mfa_required``: confirm with one of ``methods``. ``mfa_enrollment_required``: 2FA
    # is enforced and the account has no factor; set one up (``methods``) to sign in.
    status: Literal["mfa_required", "mfa_enrollment_required"]
    methods: list[MfaMethod]
    expires_at: datetime


class PasskeyRead(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    # Synced between devices (e.g. via a password manager or platform account).
    backed_up: bool


class MfaStatus(BaseModel):
    # False for accounts without a local password (sign-in via SSO/LDAP): their provider
    # is responsible for the second factor.
    available: bool
    # The admin requires a second factor for this account.
    enforced: bool
    # Passkeys can be used on this instance (WebAuthn relying party configured).
    passkeys_configured: bool
    totp: bool
    passkeys: list[PasskeyRead]
    recovery_codes_remaining: int


class TotpSetup(BaseModel):
    # Base32 secret for manual entry; the same as in ``uri``.
    secret: str
    # otpauth:// URI (issuer "ollamail", account = e-mail address).
    uri: str
    # QR code of ``uri`` as SVG data URI, rendered on the server (no external service).
    qr_svg: str


class CodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class MfaVerifyRequest(BaseModel):
    method: Literal["totp", "recovery"]
    code: str = Field(min_length=1, max_length=64)


class PasskeyRegistration(BaseModel):
    name: PasskeyName
    credential: WebAuthnCredential


class PasskeyAssertion(BaseModel):
    credential: WebAuthnCredential


class MfaEnrolled(BaseModel):
    # Shown once: generated with the first factor of an account, otherwise null.
    recovery_codes: list[str] | None
    # Set when the enrolment completed a login (2FA enforced): the user is signed in now.
    user: UserRead | None = None


class PasskeyEnrolled(MfaEnrolled):
    passkey: PasskeyRead


class RecoveryCodes(BaseModel):
    codes: list[str]

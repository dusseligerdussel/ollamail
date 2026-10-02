"""WebAuthn ceremonies with ``py_webauthn`` (``webauthn`` package).

The relying party comes from the configuration (``OLLAMAIL_AUTH_WEBAUTHN_RP_ID``,
``OLLAMAIL_AUTH_WEBAUTHN_ORIGINS``, otherwise ``OLLAMAIL_AUTH_PUBLIC_URL``), never from
request headers. Attestation is not requested (``none``): which authenticator a user
picks is their choice, the instance only needs the public key.
"""

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import AuthSettings

RP_NAME = "ollamail"
TIMEOUT_MS = 120_000
_TRANSPORTS = {transport.value for transport in AuthenticatorTransport}


class PasskeyError(Exception):
    """The browser's response did not verify (wrong origin, challenge, signature, ...)."""


@dataclass(frozen=True)
class RelyingParty:
    id: str
    origins: list[str]


def relying_party(settings: AuthSettings) -> RelyingParty | None:
    """The configured relying party, or ``None`` if passkeys are not configured."""
    origins = settings.webauthn_origins or ([settings.public_url] if settings.public_url else [])
    rp_id = settings.webauthn_rp_id or (urlsplit(origins[0]).hostname if origins else None)
    if not rp_id or not origins:
        return None
    return RelyingParty(id=rp_id, origins=list(origins))


@dataclass(frozen=True)
class StoredCredential:
    credential_id: bytes
    transports: list[str]


def _descriptors(credentials: list[StoredCredential]) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(
            id=c.credential_id,
            transports=[AuthenticatorTransport(t) for t in c.transports if t in _TRANSPORTS],
        )
        for c in credentials
    ]


def registration_options(
    rp: RelyingParty,
    *,
    user_id: uuid.UUID,
    user_name: str,
    display_name: str,
    challenge: bytes,
    existing: list[StoredCredential],
) -> dict[str, Any]:
    """``PublicKeyCredentialCreationOptions`` as JSON for ``navigator.credentials.create``.

    Discoverable credentials are preferred so a passkey also works without a password.
    The user handle is the user ID (opaque, no e-mail address).
    """
    options = generate_registration_options(
        rp_id=rp.id,
        rp_name=RP_NAME,
        user_id=user_id.bytes,
        user_name=user_name,
        user_display_name=display_name,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=_descriptors(existing),
    )
    return options_to_json_dict(options)


@dataclass(frozen=True)
class NewCredential:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    backed_up: bool
    transports: list[str]


def verify_registration(
    rp: RelyingParty, credential: dict[str, Any], challenge: bytes
) -> NewCredential:
    try:
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origins,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError(type(exc).__name__) from None
    response = credential.get("response")
    raw = response.get("transports") if isinstance(response, dict) else None
    transports = [t for t in raw if t in _TRANSPORTS] if isinstance(raw, list) else []
    return NewCredential(
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        backed_up=verified.credential_backed_up,
        transports=transports[:8],
    )


def authentication_options(
    rp: RelyingParty,
    *,
    challenge: bytes,
    allowed: list[StoredCredential],
    passwordless: bool,
) -> dict[str, Any]:
    """``PublicKeyCredentialRequestOptions`` as JSON for ``navigator.credentials.get``.

    As second factor the user's credentials are listed; passwordless sign-in lists none
    (the authenticator offers its discoverable credentials) and requires user verification.
    """
    options = generate_authentication_options(
        rp_id=rp.id,
        challenge=challenge,
        timeout=TIMEOUT_MS,
        allow_credentials=_descriptors(allowed),
        user_verification=(
            UserVerificationRequirement.REQUIRED
            if passwordless
            else UserVerificationRequirement.DISCOURAGED
        ),
    )
    return options_to_json_dict(options)


def credential_id_of(credential: dict[str, Any]) -> bytes | None:
    """The raw credential ID a browser response names (unverified)."""
    raw_id = credential.get("rawId") or credential.get("id")
    if not isinstance(raw_id, str):
        return None
    try:
        return base64url_to_bytes(raw_id)
    except ValueError:
        return None


def verify_authentication(
    rp: RelyingParty,
    credential: dict[str, Any],
    challenge: bytes,
    *,
    public_key: bytes,
    sign_count: int,
    passwordless: bool,
) -> int:
    """Verify an assertion; returns the new signature counter."""
    try:
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=rp.id,
            expected_origin=rp.origins,
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=passwordless,
        )
    except (WebAuthnException, ValueError, KeyError, TypeError) as exc:
        raise PasskeyError(type(exc).__name__) from None
    return verified.new_sign_count

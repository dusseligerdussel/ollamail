"""Errors of the SAML login. Codes are static, safe to log and shown on the login page."""

import enum


class SAMLErrorCode(enum.StrEnum):
    # The provider settings are unusable (e.g. no valid IdP certificate).
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    # The IdP answered with a non-success status (e.g. the user cancelled, access denied).
    IDP_ERROR = "idp_error"
    # The SAML response failed validation (signature, audience, destination, time window,
    # InResponseTo, replay, missing subject).
    INVALID_RESPONSE = "invalid_response"


class SAMLError(Exception):
    def __init__(self, code: SAMLErrorCode, reason: str | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        # Static detail for the log (which check failed); never response content.
        self.reason = reason or code.value


class SAMLMetadataError(ValueError):
    """IdP metadata could not be loaded or used; the message is static and safe to show."""

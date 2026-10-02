"""Errors of the OIDC login. Codes are static, safe to log and shown on the login page."""

import enum


class OIDCErrorCode(enum.StrEnum):
    # Discovery document or signing keys unavailable or invalid.
    DISCOVERY_FAILED = "provider_unavailable"
    # The IdP returned an error to the callback (e.g. the user cancelled).
    IDP_ERROR = "idp_error"
    # The code exchange failed.
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    # The ID token is invalid (signature, issuer, audience, expiry, nonce, tenant, domain).
    INVALID_TOKEN = "invalid_token"


class OIDCError(Exception):
    def __init__(self, code: OIDCErrorCode, reason: str | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        # Static detail for the log (which check failed); never claim values.
        self.reason = reason or code.value

"""SAML 2.0 login, SP-initiated: HTTP-Redirect binding to the IdP, HTTP-POST back.

``authorization_url`` sends an AuthnRequest whose ID is derived from the nonce of the
shared redirect flow (``app.auth.redirect_flow``); the flow ``state`` travels as
``RelayState``. ``complete`` validates the response with python3-saml in strict mode:

* XML schema, no DTD or entities (no XXE), exactly one assertion
* XML signature of the response or the assertion with a configured IdP certificate,
  protection against signature wrapping, no SHA-1
* issuer, ``NotBefore``/``NotOnOrAfter`` of conditions and subject confirmation (with
  the library's clock skew of five minutes), audience restriction, destination

On top of that, stricter than the library: the response must answer *this* login
(``InResponseTo`` on the response and in the signed subject confirmation, no unsolicited
responses), ``Destination``, ``Recipient`` and the audience must be present and exact,
and each assertion ID is accepted once (``replay``). Library error messages quote the
response and are never logged; only static reasons are.
"""

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from authlib.common.urls import add_params_to_uri
from onelogin.saml2.authn_request import OneLogin_Saml2_Authn_Request
from onelogin.saml2.constants import OneLogin_Saml2_Constants as Constants
from onelogin.saml2.errors import OneLogin_Saml2_ValidationError
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProviderKind, VerifiedIdentity
from app.auth.providers.saml import replay
from app.auth.providers.saml.config import SAMLConfig, login_path
from app.auth.providers.saml.errors import SAMLError, SAMLErrorCode
from app.auth.providers.saml.presets import NAMEID_EMAIL, NAMEID_TRANSIENT
from app.auth.provisioning import MAX_GROUPS, ProvisioningPolicy
from app.auth.redirect_flow import FLOW_LIFETIME_SECONDS
from app.core.logging import get_logger

log = get_logger(__name__)

# Base64 of the response; real ones are a few KiB, large group lists some 100 KiB.
MAX_RESPONSE_LENGTH = 1024 * 1024
_MAX_SUBJECT = 255
_CLOCK_SKEW = timedelta(seconds=Constants.ALLOWED_CLOCK_DRIFT)

# Validation error number → static reason for the log (e.g. "invalid_signature").
_REASONS: dict[int, str] = {
    value: name.lower()
    for name, value in vars(OneLogin_Saml2_ValidationError).items()
    if name.isupper() and isinstance(value, int)
}


def request_id(nonce: str) -> str:
    """AuthnRequest ID for the flow nonce (an XML ID must not start with a digit)."""
    return "_" + hashlib.sha256(nonce.encode()).hexdigest()


def request_data(acs_url: str, saml_response: str | None = None) -> dict[str, Any]:
    """The request as python3-saml sees it: the ACS URL is the current URL, so the
    library's destination and recipient checks compare against it."""
    parts = urlsplit(acs_url)
    return {
        "https": "on" if parts.scheme == "https" else "off",
        "http_host": parts.netloc,
        "script_name": parts.path,
        "get_data": {},
        "post_data": {} if saml_response is None else {"SAMLResponse": saml_response},
    }


class _AuthnRequest(OneLogin_Saml2_Authn_Request):  # type: ignore[misc]
    """AuthnRequest with an ID chosen by the caller instead of a random one."""

    def __init__(self, settings: OneLogin_Saml2_Settings, request_id: str) -> None:
        self._fixed_id = request_id
        super().__init__(settings)

    def _generate_request_id(self) -> str:
        return self._fixed_id


def _invalid(reason: str) -> SAMLError:
    return SAMLError(SAMLErrorCode.INVALID_RESPONSE, reason)


def _first(values: object) -> str | None:
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class SAMLProvider:
    kind = AuthProviderKind.REDIRECT

    def __init__(
        self,
        config: SAMLConfig,
        *,
        sp_entity_id: str | None = None,
        db: AsyncSession | None = None,
    ) -> None:
        """``sp_entity_id`` and ``db`` (replay cache) are needed for a login; the login
        page lists providers without them."""
        self.config = config
        self.name = config.provider_name
        self.display_name = config.display_name
        self.login_path = login_path(config.name)
        self.sp_entity_id = sp_entity_id
        self._db = db

    @property
    def provisioning(self) -> ProvisioningPolicy:
        return self.config.policy

    def settings(self, acs_url: str) -> OneLogin_Saml2_Settings:
        if self.sp_entity_id is None:
            raise SAMLError(SAMLErrorCode.PROVIDER_UNAVAILABLE, "unbound")
        try:
            return OneLogin_Saml2_Settings(
                self.config.library_settings(sp_entity_id=self.sp_entity_id, acs_url=acs_url)
            )
        except Exception:
            raise SAMLError(SAMLErrorCode.PROVIDER_UNAVAILABLE, "settings") from None

    # -- RedirectAuthProvider ---------------------------------------------------------

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, code_verifier: str
    ) -> str:
        request = _AuthnRequest(self.settings(redirect_uri), request_id(nonce))
        url: str = add_params_to_uri(
            self.config.idp_sso_url,
            [("SAMLRequest", request.get_request()), ("RelayState", state)],
        )
        return url

    async def complete(
        self, *, params: Mapping[str, str], nonce: str, redirect_uri: str, code_verifier: str
    ) -> VerifiedIdentity:
        """Validate the posted ``SAMLResponse``; ``redirect_uri`` is the ACS URL."""
        saml_response = params.get("SAMLResponse")
        if not saml_response:
            raise _invalid("missing_response")
        if len(saml_response) > MAX_RESPONSE_LENGTH:
            raise _invalid("too_large")
        expected_id = request_id(nonce)
        settings = self.settings(redirect_uri)
        try:
            response = OneLogin_Saml2_Response(settings, saml_response)
            response.is_valid(
                request_data(redirect_uri, saml_response), expected_id, raise_exceptions=True
            )
        except OneLogin_Saml2_ValidationError as exc:
            if exc.code == OneLogin_Saml2_ValidationError.STATUS_CODE_IS_NOT_SUCCESS:
                raise SAMLError(SAMLErrorCode.IDP_ERROR) from None
            raise _invalid(_REASONS.get(exc.code, "validation")) from None
        except Exception:
            # Not base64, not XML, DTD/entities, encrypted assertion, ...
            raise _invalid("malformed") from None

        self._check_binding(response, expected_id, redirect_uri)
        identity = self._identity(response)
        await self._check_replay(response)
        return identity

    # -- Checks beyond python3-saml ---------------------------------------------------

    def _check_binding(self, response: Any, expected_id: str, acs_url: str) -> None:
        """The response answers this login, is meant for this SP and arrived at the ACS."""
        if response.get_in_response_to() != expected_id:
            raise _invalid("wrong_inresponseto")
        if response.document.get("Destination") != acs_url:
            raise _invalid("wrong_destination")
        audiences = response.get_audiences()
        if not audiences or self.sp_entity_id not in audiences:
            raise _invalid("wrong_audience")
        # The response element may be unsigned (only the assertion is); the subject
        # confirmation inside the signed assertion must name this login and this ACS.
        # ``_query_assertion`` only searches the assertion the signature covers.
        confirmations = response._query_assertion("/saml:Subject/saml:SubjectConfirmation")
        for confirmation in confirmations:
            if confirmation.get("Method") != Constants.CM_BEARER:
                continue
            data = confirmation.find("saml:SubjectConfirmationData", namespaces=Constants.NSMAP)
            if (
                data is not None
                and data.get("InResponseTo") == expected_id
                and data.get("Recipient") == acs_url
                and data.get("NotOnOrAfter")
            ):
                return
        raise _invalid("wrong_subjectconfirmation")

    async def _check_replay(self, response: Any) -> None:
        if self._db is None:
            raise SAMLError(SAMLErrorCode.PROVIDER_UNAVAILABLE, "unbound")
        assertion_id = response.get_assertion_id()
        if not assertion_id:
            raise _invalid("missing_assertion_id")
        now = datetime.now(UTC)
        # The flow cookie (and so the matching InResponseTo) lives FLOW_LIFETIME_SECONDS;
        # the assertion itself may say longer.
        valid_until = now + timedelta(seconds=FLOW_LIFETIME_SECONDS)
        not_on_or_after = response.get_assertion_not_on_or_after()
        if not_on_or_after:
            valid_until = max(valid_until, datetime.fromtimestamp(not_on_or_after, UTC))
        if not await replay.remember(self._db, self.name, assertion_id, valid_until + _CLOCK_SKEW):
            raise _invalid("replay")
        # Committed now, so a failed provisioning step cannot roll it back.
        await self._db.commit()

    # -- Attributes -------------------------------------------------------------------

    def _identity(self, response: Any) -> VerifiedIdentity:
        attributes: dict[str, Any] = response.get_attributes() or {}
        config = self.config
        try:
            name_id = response.get_nameid()
            name_id_format = response.get_nameid_format()
        except OneLogin_Saml2_ValidationError:
            # No NameID (allowed with a subject attribute) or an encrypted one.
            name_id = name_id_format = None

        if config.subject_attribute:
            subject = _first(attributes.get(config.subject_attribute))
        elif name_id_format == NAMEID_TRANSIENT:
            # Changes at every login; would create a new account each time.
            raise _invalid("transient_name_id")
        else:
            subject = name_id.strip() if isinstance(name_id, str) else None
        if not subject:
            raise _invalid("missing_subject")
        if len(subject) > _MAX_SUBJECT:
            raise _invalid("subject_too_long")

        email = _first(attributes.get(config.email_attribute)) if config.email_attribute else None
        if email is None and name_id_format == NAMEID_EMAIL and isinstance(name_id, str):
            email = name_id.strip() or None
        display_name = (
            _first(attributes.get(config.display_name_attribute))
            if config.display_name_attribute
            else None
        )
        groups: frozenset[str] = frozenset()
        if config.groups_attribute:
            values = attributes.get(config.groups_attribute) or []
            groups = frozenset(v.strip() for v in values[: MAX_GROUPS * 2] if isinstance(v, str))
        return VerifiedIdentity(
            provider=self.name,
            subject=subject,
            email=email,
            display_name=display_name,
            groups=frozenset(g for g in groups if g),
            email_verified=config.trust_email and email is not None,
        )

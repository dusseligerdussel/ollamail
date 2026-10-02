"""A SAML identity provider for tests: issues responses signed with its own key.

Signing uses python3-saml (xmlsec), the same library ollamail validates with; the
templates follow what Entra ID, AD FS and Keycloak send (signed assertion, bearer
subject confirmation, audience restriction).
"""

import base64
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape, quoteattr

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from onelogin.saml2.constants import OneLogin_Saml2_Constants as Constants
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from app.auth.providers.saml.presets import NAMEID_PERSISTENT

IDP_ENTITY_ID = "https://idp.example.org/saml"
IDP_SSO_URL = "https://idp.example.org/saml/sso"
_ATTRNAME_FORMAT = "urn:oasis:names:tc:SAML:2.0:attrname-format:unspecified"
_XS = 'xmlns:xs="http://www.w3.org/2001/XMLSchema"'
_XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


@dataclass(frozen=True)
class KeyPair:
    key_pem: str
    cert_pem: str

    @property
    def cert_b64(self) -> str:
        return "".join(self.cert_pem.strip().splitlines()[1:-1])


def make_key_pair(common_name: str = "Test IdP") -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return KeyPair(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_authn_request(url: str) -> tuple[str, str, str]:
    """``(request ID, RelayState, AuthnRequest XML)`` of a HTTP-Redirect binding URL."""
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15).decode()
    request_id = xml.split(' ID="', 1)[1].split('"', 1)[0]
    return request_id, query["RelayState"], xml


@dataclass
class FakeIdP:
    """Builds responses; every keyword of ``response`` breaks one aspect on purpose."""

    keys: KeyPair = field(default_factory=make_key_pair)
    entity_id: str = IDP_ENTITY_ID
    sso_url: str = IDP_SSO_URL

    def metadata(self, *, sso_binding: str = Constants.BINDING_HTTP_REDIRECT) -> str:
        return f"""<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#" entityID={quoteattr(self.entity_id)}>
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo><ds:X509Data><ds:X509Certificate>{self.keys.cert_b64}</ds:X509Certificate></ds:X509Data></ds:KeyInfo>
    </md:KeyDescriptor>
    <md:NameIDFormat>{NAMEID_PERSISTENT}</md:NameIDFormat>
    <md:SingleSignOnService Binding="{sso_binding}" Location={quoteattr(self.sso_url)}/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""

    def response(
        self,
        *,
        in_response_to: str | None,
        acs_url: str,
        audience: str,
        name_id: str | None = "u-4711",
        name_id_format: str = NAMEID_PERSISTENT,
        attributes: dict[str, list[str]] | None = None,
        destination: str | None = "",
        recipient: str | None = "",
        assertion_in_response_to: str | None = "",
        issuer: str | None = None,
        assertion_id: str | None = None,
        issued_at: datetime | None = None,
        not_before: datetime | None = None,
        not_on_or_after: datetime | None = None,
        status: str = "urn:oasis:names:tc:SAML:2.0:status:Success",
        sign_assertion: bool = True,
        sign_response: bool = False,
        signing_keys: KeyPair | None = None,
        sign_algorithm: str = Constants.RSA_SHA256,
        digest_algorithm: str = Constants.SHA256,
        tamper: tuple[str, str] | None = None,
        doctype: str = "",
        raw: bool = False,
    ) -> str:
        """Base64 SAMLResponse (or the XML with ``raw``).

        ``""`` for destination/recipient/assertion_in_response_to means "the correct
        value", ``None`` leaves the attribute out. ``tamper`` replaces text after signing.
        """
        now = issued_at or datetime.now(UTC)
        not_before = not_before or now - timedelta(minutes=1)
        not_on_or_after = not_on_or_after or now + timedelta(minutes=5)
        issuer = issuer or self.entity_id
        assertion_id = assertion_id or f"_a{uuid.uuid4().hex}"
        response_id = f"_r{uuid.uuid4().hex}"
        destination = acs_url if destination == "" else destination
        recipient = acs_url if recipient == "" else recipient
        if assertion_in_response_to == "":
            assertion_in_response_to = in_response_to
        keys = signing_keys or self.keys

        def attr(name: str, value: str | None) -> str:
            return "" if value is None else f" {name}={quoteattr(value)}"

        attribute_xml = "".join(
            f'<saml:Attribute Name={quoteattr(name)} NameFormat="{_ATTRNAME_FORMAT}">'
            + "".join(
                f'<saml:AttributeValue {_XS} {_XSI} xsi:type="xs:string">'
                f"{escape(v)}</saml:AttributeValue>"
                for v in values
            )
            + "</saml:Attribute>"
            for name, values in (attributes or {}).items()
        )
        name_id_xml = (
            ""
            if name_id is None
            else f"<saml:NameID Format={quoteattr(name_id_format)}>{escape(name_id)}</saml:NameID>"
        )
        assertion = (
            f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="{assertion_id}" Version="2.0" IssueInstant="{_time(now)}">'
            f"<saml:Issuer>{escape(issuer)}</saml:Issuer>"
            f"<saml:Subject>{name_id_xml}"
            f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
            f"<saml:SubjectConfirmationData{attr('InResponseTo', assertion_in_response_to)}"
            f' NotOnOrAfter="{_time(not_on_or_after)}"{attr("Recipient", recipient)}/>'
            f"</saml:SubjectConfirmation></saml:Subject>"
            f'<saml:Conditions NotBefore="{_time(not_before)}"'
            f' NotOnOrAfter="{_time(not_on_or_after)}">'
            f"<saml:AudienceRestriction><saml:Audience>{escape(audience)}</saml:Audience>"
            f"</saml:AudienceRestriction></saml:Conditions>"
            f'<saml:AuthnStatement AuthnInstant="{_time(now)}" SessionIndex="_s1">'
            f"<saml:AuthnContext><saml:AuthnContextClassRef>"
            f"urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
            f"</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>"
            + (
                f"<saml:AttributeStatement>{attribute_xml}</saml:AttributeStatement>"
                if attribute_xml
                else ""
            )
            + "</saml:Assertion>"
        )
        if sign_assertion:
            assertion = self._sign(assertion, keys, sign_algorithm, digest_algorithm)
        response = (
            f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{response_id}" '
            f'Version="2.0" IssueInstant="{_time(now)}"{attr("Destination", destination)}'
            f"{attr('InResponseTo', in_response_to)}>"
            f"<saml:Issuer>{escape(issuer)}</saml:Issuer>"
            f'<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>'
            f"{assertion}</samlp:Response>"
        )
        if sign_response:
            response = self._sign(response, keys, sign_algorithm, digest_algorithm)
        if tamper:
            response = response.replace(*tamper)
        response = doctype + response
        if raw:
            return response
        return base64.b64encode(response.encode()).decode()

    @staticmethod
    def _sign(xml: str, keys: KeyPair, sign_algorithm: str, digest_algorithm: str) -> str:
        signed = OneLogin_Saml2_Utils.add_sign(
            xml,
            keys.key_pem,
            keys.cert_pem,
            sign_algorithm=sign_algorithm,
            digest_algorithm=digest_algorithm,
        )
        text = signed.decode() if isinstance(signed, bytes) else str(signed)
        # Drop the XML declaration; the part is embedded in the response.
        return text.split("?>", 1)[1].strip() if text.startswith("<?xml") else text

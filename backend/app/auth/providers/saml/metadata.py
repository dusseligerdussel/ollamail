"""IdP metadata: fetch it from a URL or take an upload, extract what the login needs.

Parsing uses python3-saml (``OneLogin_Saml2_IdPMetadataParser``), whose XML parser
rejects DTDs and entity declarations (no XXE, no entity expansion). Fetching is done here
instead of by the library: HTTPS only (``http`` for localhost), certificate validation,
no redirects, a timeout and a size limit.
"""

from dataclasses import dataclass

import httpx
from onelogin.saml2.constants import OneLogin_Saml2_Constants as Constants
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

from app.auth.providers.saml.config import normalize_certificate, normalize_url
from app.auth.providers.saml.errors import SAMLMetadataError
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT_SECONDS = 10.0
# Federation metadata of AD FS or Entra ID is well below this.
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_CERTIFICATES = 10
# Keycloak answers 406 to the metadata media type alone.
_ACCEPT = "application/samlmetadata+xml, application/xml;q=0.9, */*;q=0.8"


@dataclass(frozen=True)
class IdPMetadata:
    entity_id: str
    sso_url: str
    certificates: tuple[str, ...]


def parse_metadata(xml: str | bytes) -> IdPMetadata:
    """Entity ID, SSO URL (HTTP-Redirect binding) and signing certificates of the first
    IdP in ``xml``. Raises ``SAMLMetadataError``."""
    if len(xml) > MAX_METADATA_BYTES:
        raise SAMLMetadataError("The metadata is too large.")
    try:
        data = OneLogin_Saml2_IdPMetadataParser.parse(
            xml, required_sso_binding=Constants.BINDING_HTTP_REDIRECT
        )
    except Exception:
        # Malformed XML, a DTD or entity declarations; the parser's message may quote
        # the document, so it is not passed on.
        raise SAMLMetadataError("The metadata is not valid SAML metadata.") from None
    idp = data.get("idp")
    if not isinstance(idp, dict) or not idp.get("entityId"):
        raise SAMLMetadataError("The metadata contains no identity provider.")
    sso = idp.get("singleSignOnService")
    if not isinstance(sso, dict) or not sso.get("url"):
        raise SAMLMetadataError(
            "The metadata has no single sign-on endpoint with the HTTP-Redirect binding."
        )
    try:
        sso_url = normalize_url(str(sso["url"]))
    except ValueError:
        raise SAMLMetadataError("The single sign-on endpoint must be an https URL.") from None
    if "x509cert" in idp:
        raw = [idp["x509cert"]]
    else:
        multi = idp.get("x509certMulti")
        raw = list(multi.get("signing", [])) if isinstance(multi, dict) else []
    try:
        certificates = tuple(dict.fromkeys(normalize_certificate(str(c)) for c in raw))
    except ValueError:
        raise SAMLMetadataError("The metadata contains an invalid certificate.") from None
    if not certificates:
        raise SAMLMetadataError("The metadata contains no signing certificate.")
    return IdPMetadata(
        entity_id=str(idp["entityId"]),
        sso_url=sso_url,
        certificates=certificates[:MAX_CERTIFICATES],
    )


async def fetch_metadata(url: str) -> IdPMetadata:
    """Download and parse the metadata at ``url``. Raises ``SAMLMetadataError``."""
    try:
        url = normalize_url(url)
    except ValueError:
        raise SAMLMetadataError("The metadata URL must be an https URL.") from None
    try:
        async with (
            httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False) as http,
            http.stream("GET", url, headers={"Accept": _ACCEPT}) as resp,
        ):
            if resp.status_code != 200:
                log.info("saml_metadata_rejected", status=resp.status_code)
                raise SAMLMetadataError("The metadata URL did not return the metadata.")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_METADATA_BYTES:
                    raise SAMLMetadataError("The metadata is too large.")
    except httpx.HTTPError:
        raise SAMLMetadataError("The metadata URL is not reachable.") from None
    return parse_metadata(bytes(body))

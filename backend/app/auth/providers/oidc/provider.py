"""OpenID Connect login: Authorization Code flow with PKCE (S256), ``state`` and ``nonce``.

``authorization_url`` builds the redirect to the IdP; ``complete`` exchanges the code at the
token endpoint (back channel, client authentication) and validates the ID token:

* signature with the issuer's published keys, asymmetric algorithms only (no ``none``/HMAC)
* ``iss`` equals the configured issuer (Entra multi-tenant: the template filled with ``tid``)
* ``aud`` contains the client ID; with several audiences ``azp`` must be the client ID
* ``exp``, ``iat`` and ``nbf`` with 60 seconds leeway
* ``nonce`` equals the value bound to the browser that started the login
* preset checks: Entra ``tid`` (allowed tenants), Google ``hd`` (hosted domains)

JWT parsing and signature checks are done by ``joserfc``; PKCE and client authentication
helpers come from Authlib. Token values and claims never reach the logs.
"""

import hmac
from collections.abc import Mapping
from typing import Any

import httpx
from authlib.common.urls import add_params_to_uri
from authlib.oauth2.auth import ClientAuth
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import (
    BadSignatureError,
    ExpiredTokenError,
    InvalidClaimError,
    InvalidKeyIdError,
    JoseError,
    MissingClaimError,
    UnsupportedAlgorithmError,
)

from app.auth.providers.base import AuthProviderKind, VerifiedIdentity
from app.auth.providers.oidc.config import OIDCConfig
from app.auth.providers.oidc.errors import OIDCError, OIDCErrorCode
from app.auth.providers.oidc.metadata import MetadataCache, ProviderMetadata
from app.auth.providers.oidc.presets import (
    ENTRA_MULTI_TENANT,
    ENTRA_TENANT_PLACEHOLDER,
    GOOGLE_ISSUERS,
    OIDCPreset,
    entra_tenant,
)
from app.auth.provisioning import ProvisioningPolicy
from app.core.logging import get_logger

log = get_logger(__name__)

LEEWAY_SECONDS = 60
_MAX_SUBJECT = 255
_MAX_TOKEN_RESPONSE_BYTES = 256 * 1024


def _invalid(reason: str) -> OIDCError:
    return OIDCError(OIDCErrorCode.INVALID_TOKEN, reason)


def _is_true(value: object) -> bool:
    # Some IdPs send booleans as strings.
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


class OIDCProvider:
    kind = AuthProviderKind.REDIRECT

    def __init__(self, config: OIDCConfig, metadata: MetadataCache) -> None:
        self.config = config
        self.name = config.provider_name
        self.display_name = config.display_name
        self.login_path = f"/auth/oidc/{config.name}/login"
        self.callback_path = f"/auth/oidc/{config.name}/callback"
        self._cache = metadata

    @property
    def provisioning(self) -> ProvisioningPolicy:
        return self.config.policy

    # -- Entra multi-tenant -----------------------------------------------------------

    @property
    def _entra_tenant(self) -> str | None:
        if self.config.preset is not OIDCPreset.ENTRA:
            return None
        return entra_tenant(self.config.issuer)

    @property
    def _multi_tenant(self) -> bool:
        return self._entra_tenant in ENTRA_MULTI_TENANT

    def _discovery_issuer(self) -> str | None:
        if self._multi_tenant:
            return f"https://login.microsoftonline.com/{ENTRA_TENANT_PLACEHOLDER}/v2.0"
        return None

    async def metadata(self) -> ProviderMetadata:
        return await self._cache.metadata(
            self.config.issuer, expected_issuer=self._discovery_issuer()
        )

    # -- RedirectAuthProvider ---------------------------------------------------------

    async def authorization_url(
        self, *, state: str, nonce: str, redirect_uri: str, code_verifier: str
    ) -> str:
        metadata = await self.metadata()
        params = [
            ("response_type", "code"),
            ("client_id", self.config.client_id),
            ("redirect_uri", redirect_uri),
            ("scope", " ".join(self.config.scopes)),
            ("state", state),
            ("nonce", nonce),
            ("code_challenge", create_s256_code_challenge(code_verifier)),
            ("code_challenge_method", "S256"),
        ]
        if self.config.preset is OIDCPreset.GOOGLE and len(self.config.hosted_domains) == 1:
            # Only a hint for the account chooser; the hd claim is still checked.
            params.append(("hd", next(iter(self.config.hosted_domains))))
        url: str = add_params_to_uri(metadata.authorization_endpoint, params)
        return url

    async def complete(
        self, *, params: Mapping[str, str], nonce: str, redirect_uri: str, code_verifier: str
    ) -> VerifiedIdentity:
        if "error" in params:
            raise OIDCError(OIDCErrorCode.IDP_ERROR)
        code = params.get("code")
        if not code:
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "missing_code")
        metadata = await self.metadata()
        tokens = await self._exchange(metadata, code, redirect_uri, code_verifier)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "missing_id_token")
        claims = await self.validate_id_token(metadata, id_token, nonce)
        access_token = tokens.get("access_token")
        if isinstance(access_token, str) and self._needs_userinfo(claims, metadata):
            claims = await self._merge_userinfo(metadata, access_token, claims)
        return self._identity(claims)

    async def logout_url(self, post_logout_redirect_uri: str) -> str | None:
        """RP-initiated logout URL, if the IdP supports it (``end_session_endpoint``)."""
        try:
            metadata = await self.metadata()
        except OIDCError:
            return None
        if metadata.end_session_endpoint is None:
            return None
        url: str = add_params_to_uri(
            metadata.end_session_endpoint,
            [
                ("client_id", self.config.client_id),
                ("post_logout_redirect_uri", post_logout_redirect_uri),
            ],
        )
        return url

    # -- Token endpoint ---------------------------------------------------------------

    def _auth_method(self, metadata: ProviderMetadata) -> str:
        if not self.config.client_secret:
            return "none"
        methods = metadata.token_endpoint_auth_methods
        if methods and "client_secret_basic" not in methods and "client_secret_post" in methods:
            return "client_secret_post"
        return "client_secret_basic"

    async def _exchange(
        self, metadata: ProviderMetadata, code: str, redirect_uri: str, code_verifier: str
    ) -> dict[str, Any]:
        body = httpx.QueryParams(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        auth = ClientAuth(
            self.config.client_id, self.config.client_secret, self._auth_method(metadata)
        )
        url, headers, content = auth.prepare("POST", metadata.token_endpoint, headers, str(body))
        try:
            async with self._cache.client() as http:
                response = await http.post(url, headers=headers, content=content)
        except httpx.HTTPError:
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "unreachable") from None
        if response.status_code != 200 or len(response.content) > _MAX_TOKEN_RESPONSE_BYTES:
            # The body may echo request data; only the status is logged.
            log.info(
                "oidc_token_exchange_rejected", provider=self.name, status=response.status_code
            )
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "rejected")
        try:
            tokens = response.json()
        except ValueError:
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "malformed") from None
        if not isinstance(tokens, dict):
            raise OIDCError(OIDCErrorCode.TOKEN_EXCHANGE_FAILED, "malformed")
        return tokens

    # -- ID token ---------------------------------------------------------------------

    async def _decode(self, metadata: ProviderMetadata, id_token: str) -> dict[str, Any]:
        algorithms = list(metadata.signing_algorithms)
        keys = await self._cache.keys(metadata)
        try:
            try:
                token = jwt.decode(id_token, keys, algorithms=algorithms)
            except InvalidKeyIdError:
                # Unknown key ID: the IdP may have rotated its keys.
                keys = await self._cache.keys(metadata, refresh=True)
                token = jwt.decode(id_token, keys, algorithms=algorithms)
        except (BadSignatureError, InvalidKeyIdError):
            raise _invalid("signature") from None
        except UnsupportedAlgorithmError:
            raise _invalid("algorithm") from None
        except (JoseError, ValueError):
            raise _invalid("malformed") from None
        return dict(token.claims)

    def _expected_issuer(self, claims: Mapping[str, Any]) -> str:
        if not self._multi_tenant:
            return self.config.issuer
        tid = _str(claims.get("tid"))
        if tid is None:
            raise _invalid("tenant")
        return f"https://login.microsoftonline.com/{tid}/v2.0"

    async def validate_id_token(
        self, metadata: ProviderMetadata, id_token: str, nonce: str
    ) -> dict[str, Any]:
        claims = await self._decode(metadata, id_token)
        self._check_preset_before_issuer(claims)
        issuers = (
            list(GOOGLE_ISSUERS)
            if self.config.preset is OIDCPreset.GOOGLE
            else [self._expected_issuer(claims)]
        )
        registry = jwt.JWTClaimsRegistry(
            leeway=LEEWAY_SECONDS,
            iss={"essential": True, "values": issuers},
            sub={"essential": True},
            aud={"essential": True, "value": self.config.client_id},
            exp={"essential": True},
            iat={"essential": True},
        )
        try:
            registry.validate(claims)
        except ExpiredTokenError:
            raise _invalid("expired") from None
        except (MissingClaimError, InvalidClaimError) as exc:
            raise _invalid(f"claim_{exc.claim}") from None
        except JoseError:
            raise _invalid("claims") from None
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.config.client_id:
            raise _invalid("claim_azp")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            raise _invalid("claim_nonce")
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or len(subject) > _MAX_SUBJECT:
            raise _invalid("claim_sub")
        self._check_preset(claims)
        return claims

    def _check_preset_before_issuer(self, claims: Mapping[str, Any]) -> None:
        # The tenant decides the expected issuer, so it is checked first.
        if self.config.preset is not OIDCPreset.ENTRA:
            return
        tid = (_str(claims.get("tid")) or "").lower()
        configured = self._entra_tenant
        if self._multi_tenant:
            if tid not in self.config.allowed_tenants:
                raise _invalid("tenant")
        elif tid != configured:
            raise _invalid("tenant")

    def _check_preset(self, claims: Mapping[str, Any]) -> None:
        if self.config.preset is OIDCPreset.GOOGLE and self.config.hosted_domains:
            hd = (_str(claims.get("hd")) or "").lower()
            if hd not in self.config.hosted_domains:
                raise _invalid("hosted_domain")

    # -- Claims -----------------------------------------------------------------------

    def _needs_userinfo(self, claims: Mapping[str, Any], metadata: ProviderMetadata) -> bool:
        if metadata.userinfo_endpoint is None:
            return False
        missing_groups = self.config.groups_claim and self.config.groups_claim not in claims
        return "email" not in claims or bool(missing_groups)

    async def _merge_userinfo(
        self, metadata: ProviderMetadata, access_token: str, claims: dict[str, Any]
    ) -> dict[str, Any]:
        assert metadata.userinfo_endpoint is not None
        try:
            async with self._cache.client() as http:
                response = await http.get(
                    metadata.userinfo_endpoint,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                )
            userinfo = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            userinfo = None
        # The userinfo response must be about the same subject (OIDC Core §5.3.2).
        if not isinstance(userinfo, dict) or userinfo.get("sub") != claims["sub"]:
            log.info("oidc_userinfo_unavailable", provider=self.name)
            return claims
        merged = dict(claims)
        for key in (
            "email",
            "email_verified",
            "name",
            "given_name",
            "family_name",
            "preferred_username",
        ):
            if key not in merged and key in userinfo:
                merged[key] = userinfo[key]
        claim = self.config.groups_claim
        if claim and claim not in merged and claim in userinfo:
            merged[claim] = userinfo[claim]
        return merged

    def _email_verified(self, claims: Mapping[str, Any]) -> bool:
        if _is_true(claims.get("email_verified")):
            return True
        # Entra ID sends no email_verified; the optional claim xms_edov says whether the
        # domain of the address is verified by the tenant. Without it the address is
        # treated as unverified (it is editable in many tenants).
        return self.config.preset is OIDCPreset.ENTRA and _is_true(claims.get("xms_edov"))

    def _groups(self, claims: Mapping[str, Any]) -> frozenset[str]:
        claim = self.config.groups_claim
        if not claim:
            return frozenset()
        value = claims.get(claim)
        if value is None and "_claim_names" in claims:
            # Entra "groups overage": too many groups for the token.
            log.warning("oidc_groups_overage", provider=self.name)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return frozenset()
        return frozenset(str(v) for v in value if isinstance(v, str | int) and str(v).strip())

    def _display_name(self, claims: Mapping[str, Any]) -> str | None:
        name = _str(claims.get("name"))
        if name:
            return name
        parts = [_str(claims.get("given_name")), _str(claims.get("family_name"))]
        joined = " ".join(p for p in parts if p)
        return joined or _str(claims.get("preferred_username"))

    def _identity(self, claims: Mapping[str, Any]) -> VerifiedIdentity:
        return VerifiedIdentity(
            provider=self.name,
            subject=claims["sub"],
            email=_str(claims.get("email")),
            display_name=self._display_name(claims),
            groups=self._groups(claims),
            email_verified=self._email_verified(claims),
        )

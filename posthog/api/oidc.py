import re
import time
from functools import cached_property
from typing import Any, cast
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

import jwt
import structlog
from prometheus_client import Counter, Histogram
from requests import HTTPError, RequestException, Response, Timeout
from social_core.backends.open_id_connect import OpenIdConnectAuth
from social_core.exceptions import AuthConnectionError, AuthFailed, AuthMissingParameter, AuthTokenError
from urllib3.util import Timeout as Urllib3Timeout

from posthog.constants import AvailableFeature
from posthog.dataclasses import frozen
from posthog.models.identity_provider_config import IdentityProviderConfig
from posthog.security.pinned_requests import SSRFBlockedError, pinned_session

BasicAuthCredentials = tuple[str, str]

logger = structlog.get_logger("posthog.auth.oidc")
OIDC_REQUEST_FAILURES = Counter(
    "posthog_oidc_request_failures_total",
    "OIDC requests that failed before authentication could continue.",
    labelnames=["phase", "failure_category"],
)
OIDC_REQUEST_DURATION = Histogram(
    "posthog_oidc_request_duration_seconds",
    "OIDC request duration.",
    labelnames=["phase"],
)


OIDC_FETCH_TIMEOUT_SECONDS = 10

# Candidate claim names per canonical OIDC name, in order of preference. The SAML claim-type URIs
# appear because an ADFS relying party can emit them unchanged, and `upn` is last for email
# because ADFS always issues it while the `email` claim depends on the AD `mail` attribute being
# populated. A `upn` that is not an address still fails the verified-domain check below.
CLAIM_CANDIDATES: dict[str, tuple[str, ...]] = {
    "email": (
        "email",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "upn",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    ),
    # `unique_name` is absent on purpose. ADFS issues it as `DOMAIN\\samAccountName`, which is a
    # login identifier, not a display name. `_display_name_from_account_name` reads it separately
    # and reformats it, so it can never reach the UI in its raw form.
    "name": (
        "name",
        "Name",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    ),
    "given_name": (
        "given_name",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
    ),
    "family_name": (
        "family_name",
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
    ),
}


@frozen
class OIDCClientCredentials:
    client_id: str
    client_secret: str

    def as_tuple(self) -> BasicAuthCredentials:
        return self.client_id, self.client_secret


class MultitenantOIDCAuth(OpenIdConnectAuth):
    DEFAULT_USE_PKCE = True
    JWT_DECODE_OPTIONS = {"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]}

    @cached_property
    def identity_provider_config(self) -> IdentityProviderConfig:
        config_id = self.strategy.session_get("oidc_config_id")
        email = self.strategy.session_get("oidc_email")
        organization_id = self.strategy.session_get("oidc_organization_id")
        if not isinstance(email, str) or organization_id is None:
            raise AuthFailed(self, "OIDC email is not available.")
        try:
            config = (
                IdentityProviderConfig.objects.get_queryset()
                .oidc_for_email(email)
                .select_related("organization")
                .get(id=config_id, organization_id=organization_id)
            )
        except (IdentityProviderConfig.DoesNotExist, ValidationError, ValueError):
            raise AuthFailed(self, "OIDC configuration is not available.")
        if not config.has_oidc or not config.organization.is_feature_available(AvailableFeature.OIDC):
            raise AuthFailed(self, "OIDC is not available for this organization.")
        return config

    def auth_url(self) -> str:
        email = self.data.get("email")
        if not email or not isinstance(email, str):
            raise AuthMissingParameter(self, "email")
        configs = [
            config
            for config in IdentityProviderConfig.objects.get_queryset()
            .oidc_for_email(email)
            .select_related("organization")
            if config.has_oidc and config.organization.is_feature_available(AvailableFeature.OIDC)
        ]
        if len(configs) != 1:
            raise AuthFailed(self, "OIDC requires one configured identity provider for this email domain.")
        self.strategy.session_set("oidc_config_id", str(configs[0].id))
        self.strategy.session_set("oidc_email", email)
        self.strategy.session_set("oidc_organization_id", str(configs[0].organization_id))
        return super().auth_url()

    def oidc_endpoint(self) -> str:
        return self.identity_provider_config.oidc_issuer_url.rstrip("/")

    def oidc_config(self) -> dict[str, Any]:
        return self.discovery_document

    def get_json(self, url: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            document = super().get_json(url, *args, **kwargs)
        except (TypeError, ValueError) as error:
            raise AuthFailed(self, "The OIDC provider returned invalid JSON.") from error
        if not isinstance(document, dict):
            raise AuthFailed(self, "The OIDC provider returned an invalid JSON document.")
        return document

    @cached_property
    def discovery_document(self) -> dict[str, Any]:
        document = self.get_json(f"{self.oidc_endpoint()}/.well-known/openid-configuration")
        issuer = document.get("issuer")
        configured_issuer = self.identity_provider_config.oidc_issuer_url
        if not isinstance(issuer, str) or issuer.rstrip("/") != configured_issuer.rstrip("/"):
            raise AuthFailed(self, "The OIDC discovery issuer does not match the configured issuer.")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = document.get(field)
            if not isinstance(endpoint, str) or urlsplit(endpoint).scheme != "https":
                raise AuthFailed(self, "OIDC discovery requires HTTPS endpoints.")
        return document

    def _get_client_credentials(self) -> OIDCClientCredentials:
        config = self.identity_provider_config
        return OIDCClientCredentials(
            client_id=config.oidc_client_id,
            client_secret=config.oidc_credentials["client_secret"],
        )

    def get_key_and_secret(self) -> BasicAuthCredentials:
        return self._get_client_credentials().as_tuple()

    def get_jwks_keys(self) -> list[dict[str, Any]]:
        return self.get_remote_jwks_keys()

    def get_remote_jwks_keys(self) -> list[dict[str, Any]]:
        try:
            document = self.request(self.jwks_uri()).json()
        except (TypeError, ValueError) as error:
            raise AuthFailed(self, "The OIDC provider returned invalid JWKS JSON.") from error
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise AuthFailed(self, "The OIDC provider returned an invalid JWKS document.")
        if not all(isinstance(key, dict) for key in document["keys"]):
            raise AuthFailed(self, "The OIDC provider returned invalid JWKS keys.")
        return cast(list[dict[str, Any]], document["keys"])

    def find_valid_key(self, id_token: str) -> dict[str, Any] | None:
        try:
            key_id = jwt.get_unverified_header(id_token).get("kid")
            keys = [
                {**key, "alg": "RS256"}
                for key in self.get_jwks_keys()
                if key.get("kty") == "RSA"
                and key.get("alg", "RS256") == "RS256"
                and key.get("use", "sig") == "sig"
                and (key_id is None or key.get("kid") == key_id)
            ]
            return keys[0] if len(keys) == 1 else None
        except (AttributeError, KeyError, TypeError, ValueError, jwt.InvalidTokenError) as error:
            raise AuthTokenError(self, "The OIDC ID token or JWKS is invalid.") from error

    def validate_claims(self, id_token: dict[str, Any]) -> None:
        client_id = self.identity_provider_config.oidc_client_id
        authorized_party = id_token.get("azp")
        audiences = id_token.get("aud")
        if (authorized_party is not None and authorized_party != client_id) or (
            isinstance(audiences, list) and len(audiences) > 1 and authorized_party != client_id
        ):
            raise AuthTokenError(self, "The OIDC authorized party does not match the client ID.")
        super().validate_claims(id_token)

    def _request_phase(self, url: str) -> str:
        if url.endswith("/.well-known/openid-configuration"):
            return "discovery"
        if not hasattr(self, "strategy"):
            return "unknown"
        if url == self.access_token_url():
            return "token"
        if url == self.jwks_uri():
            return "jwks"
        if url == self.userinfo_url():
            return "userinfo"
        return "unknown"

    def _request_failure_category(self, error: RequestException | SSRFBlockedError) -> str:
        if isinstance(error, SSRFBlockedError):
            return "ssrf_blocked"
        if isinstance(error, Timeout):
            return "timeout"
        if isinstance(error, HTTPError):
            return "http_error"
        return "request_error"

    def request(self, url: str, method: str = "GET", *args: Any, **kwargs: Any) -> Response:
        if urlsplit(url).scheme != "https":
            raise AuthFailed(self, "OIDC requires HTTPS endpoints.")
        phase = self._request_phase(url)
        started_at = time.monotonic()
        kwargs["timeout"] = Urllib3Timeout(
            total=OIDC_FETCH_TIMEOUT_SECONDS,
            connect=OIDC_FETCH_TIMEOUT_SECONDS,
            read=OIDC_FETCH_TIMEOUT_SECONDS,
        )
        kwargs["allow_redirects"] = False
        kwargs["stream"] = False
        try:
            with pinned_session(url) as session:
                response = session.request(method, url, *args, **kwargs)
                try:
                    if response.is_redirect:
                        raise AuthFailed(self, "OIDC endpoint redirects are not supported.")
                    response.raise_for_status()

                    return response
                finally:
                    response.close()
        except (RequestException, SSRFBlockedError) as error:
            failure_category = self._request_failure_category(error)
            config = self.identity_provider_config
            duration_seconds = time.monotonic() - started_at
            OIDC_REQUEST_FAILURES.labels(phase, failure_category).inc()
            logger.warning(
                "oidc_request_failed",
                phase=phase,
                failure_category=failure_category,
                identity_provider_config_id=str(config.id),
                organization_id=str(config.organization_id),
                duration_seconds=duration_seconds,
            )
            raise AuthConnectionError(self) from error
        finally:
            OIDC_REQUEST_DURATION.labels(phase).observe(time.monotonic() - started_at)

    @staticmethod
    def _resolve_claim(canonical: str, userinfo: dict[str, Any], id_token: dict[str, Any]) -> Any:
        """Read a claim by its canonical OIDC name, across the spellings a provider may use.

        Two independent problems need solving here.

        OIDC Core lets a provider put identity claims in the userinfo response, the ID token, or
        both. ADFS serves only `sub` from userinfo and carries everything else in the ID token, so
        a userinfo-only read rejects a valid ADFS login. Both sources carry the same trust,
        because `validate_and_return_id_token` verifies the token signature before this runs and
        the caller matches the two `sub` values first.

        An ADFS relying party also chooses its own outgoing claim type per attribute, so the same
        value arrives as the OIDC short name, as the SAML claim-type URI, or under a hand-typed
        label, depending on who configured the trust. Each canonical name is therefore tried
        against a list of candidates, the way the SAML backend reads an assertion in
        `ee.api.authentication.MultitenantSAMLAuth._get_attr`.
        """
        for candidate in CLAIM_CANDIDATES.get(canonical, (canonical,)):
            for source in (userinfo, id_token):
                if candidate not in source:
                    continue
                value = source[candidate]
                # A multi-valued AD attribute arrives as a list.
                if isinstance(value, list):
                    value = value[0] if value else None
                if value is not None:
                    return value
        return None

    @staticmethod
    def _display_name_from_account_name(unique_name: Any) -> str | None:
        """Build a display name out of an ADFS `unique_name`, or return None.

        `posthog.api.signup.social_create_user` refuses a signup whose name is empty, so a relying
        party that issues no name claim blocks every new member. ADFS always issues `unique_name`
        as `DOMAIN\\samAccountName`, so dropping the domain prefix and splitting the account name
        on its separators gives a readable name where there would otherwise be none. The letter
        case is left alone, because a name like `McDonald` does not survive title casing. A real
        name claim is resolved first and always wins over this.
        """
        if not isinstance(unique_name, str):
            return None
        account = unique_name.rsplit("\\", 1)[-1]
        return " ".join(part for part in re.split(r"[._\-]+", account) if part) or None

    @staticmethod
    def _is_email_unverified(email_verified: Any) -> bool:
        """Whether the provider states that the email address is unverified.

        `email_verified` is OPTIONAL in OIDC Core and ADFS never sends it, so an absent claim
        cannot mean "unverified" without locking out a compliant provider. A provider that sends
        the claim is still held to it. ADFS claim rules emit strings instead of JSON booleans, so
        the string forms resolve the same way as the boolean ones.
        """
        if email_verified is None:
            return False
        if isinstance(email_verified, bool):
            return not email_verified
        if isinstance(email_verified, str):
            return email_verified.strip().lower() != "true"
        return True

    def user_data(self, access_token: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        id_token = cast(dict[str, Any] | None, self.id_token)
        if id_token is None:
            raise AuthFailed(self, "OIDC did not return a valid ID token.")
        userinfo = super().user_data(access_token, *args, **kwargs)
        if not isinstance(userinfo, dict) or userinfo.get("sub") != id_token.get("sub"):
            raise AuthFailed(self, "The OIDC user does not match the ID token.")
        email = self._resolve_claim("email", userinfo, id_token)
        if not isinstance(email, str) or self._is_email_unverified(
            self._resolve_claim("email_verified", userinfo, id_token)
        ):
            raise AuthFailed(self, "OIDC requires a verified email address from the identity provider.")
        if not (
            IdentityProviderConfig.objects.get_queryset()
            .oidc_for_email(email)
            .filter(id=self.identity_provider_config.id)
            .exists()
        ):
            raise AuthFailed(self, "The OIDC email domain does not belong to this identity provider configuration.")
        # Return the claims under their canonical names so `get_user_details` reads the values
        # this method resolved, rather than looking up one spelling in one of the two sources.
        resolved = {
            canonical: self._resolve_claim(canonical, userinfo, id_token)
            for canonical in ("name", "given_name", "family_name")
        }
        if resolved["name"] is None:
            resolved["name"] = self._display_name_from_account_name(
                self._resolve_claim("unique_name", userinfo, id_token)
            )
        return {**userinfo, **{k: v for k, v in resolved.items() if v is not None}, "email": email}

    def get_user_id(self, details: dict[str, Any], response: dict[str, Any]) -> str:
        issuer = self.identity_provider_config.oidc_issuer_url.rstrip("/")
        user_id = f"{issuer}:{response['sub']}"
        if len(user_id) > 255:
            raise AuthFailed(self, "The OIDC user identifier is too long.")
        return user_id

    def extra_data(
        self, user: Any, uid: str, response: dict[str, Any], details: dict[str, Any], *args: Any, **kwargs: Any
    ) -> dict:
        return {}

from typing import Any

from posthog.test.base import BaseTest
from unittest.mock import MagicMock, patch

from django.utils import timezone

from parameterized import parameterized
from social_core.backends.open_id_connect import OpenIdConnectAuth
from social_core.exceptions import AuthFailed

from posthog.api.oidc import MultitenantOIDCAuth
from posthog.models.identity_provider_config import ConfigScope, DomainScope, IdentityProviderConfig
from posthog.models.organization_domain import OrganizationDomain

# ADFS serves only `sub` from its userinfo endpoint and carries every other claim in the ID token.
ADFS_USERINFO = {"sub": "adfs-subject-1"}
DEFAULT_ID_TOKEN_CLAIMS = {"email": "jane@example.com", "given_name": "Jane", "family_name": "Doe"}


class TestMultitenantOIDCUserData(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        OrganizationDomain.objects.create(
            organization=self.organization, domain="example.com", verified_at=timezone.now()
        )
        self.config = IdentityProviderConfig.objects.create(
            organization=self.organization,
            name="ADFS",
            config_scope=ConfigScope.OIDC,
            domain_scope=DomainScope.ALL,
            oidc_issuer_url="https://idp.example.com/adfs",
            oidc_client_id="client-id",
        )

    def _backend_and_user_data(
        self, userinfo: dict[str, Any], id_token_claims: dict[str, Any] | None = None, **overrides: Any
    ) -> tuple[MultitenantOIDCAuth, dict[str, Any]]:
        claims = DEFAULT_ID_TOKEN_CLAIMS if id_token_claims is None else id_token_claims
        backend = MultitenantOIDCAuth(strategy=MagicMock())
        backend.id_token = {"sub": "adfs-subject-1", **claims, **overrides}
        backend.__dict__["identity_provider_config"] = self.config
        with patch.object(OpenIdConnectAuth, "user_data", return_value=userinfo):
            return backend, backend.user_data("access-token")

    def _user_data(self, userinfo: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        return self._backend_and_user_data(userinfo, None, **overrides)[1]

    def test_resolves_email_from_id_token_when_userinfo_omits_it(self) -> None:
        assert self._user_data(ADFS_USERINFO)["email"] == "jane@example.com"

    def test_userinfo_email_takes_precedence_over_the_id_token(self) -> None:
        userinfo = {**ADFS_USERINFO, "email": "jane.doe@example.com", "email_verified": True}
        assert self._user_data(userinfo)["email"] == "jane.doe@example.com"

    @parameterized.expand(
        [
            ("absent", {}, True),
            ("boolean_true", {"email_verified": True}, True),
            ("boolean_false", {"email_verified": False}, False),
            ("string_true", {"email_verified": "true"}, True),
            ("string_true_mixed_case", {"email_verified": "TRUE"}, True),
            ("string_false", {"email_verified": "false"}, False),
            ("unrecognized_type", {"email_verified": 1}, False),
        ]
    )
    def test_email_verified_claim(self, _name: str, claims: dict[str, Any], accepted: bool) -> None:
        if accepted:
            assert self._user_data(ADFS_USERINFO, **claims)["email"] == "jane@example.com"
        else:
            with self.assertRaises(AuthFailed):
                self._user_data(ADFS_USERINFO, **claims)

    @parameterized.expand(
        [
            ("oidc_short_names", {"email": "jane@example.com", "name": "Jane Doe"}, "Jane Doe"),
            ("capitalized_name", {"email": "jane@example.com", "Name": "Jane Doe"}, "Jane Doe"),
            # The shape the production ADFS relying party sends before its claim rules are in
            # place: no email and no name claim, only the two ADFS built-ins.
            (
                "adfs_builtin_claims_only",
                {"upn": "jane@example.com", "unique_name": "EXAMPLE\\Jane.Doe"},
                "Jane Doe",
            ),
            ("account_name_without_a_domain", {"upn": "jane@example.com", "unique_name": "jane_doe"}, "jane doe"),
            (
                "a_real_name_claim_beats_the_account_name",
                {"upn": "jane@example.com", "unique_name": "EXAMPLE\\Jane.Doe", "name": "Jane Q. Doe"},
                "Jane Q. Doe",
            ),
            (
                "saml_claim_type_uris",
                {
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": "jane@example.com",
                    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name": "Jane Doe",
                },
                "Jane Doe",
            ),
            ("upn_stands_in_for_a_missing_email", {"upn": "jane@example.com"}, None),
            ("multi_valued_attribute", {"email": ["jane@example.com"], "name": ["Jane Doe"]}, "Jane Doe"),
        ]
    )
    def test_resolves_claims_across_provider_spellings(
        self, _name: str, claims: dict[str, Any], fullname: str | None
    ) -> None:
        backend, response = self._backend_and_user_data(ADFS_USERINFO, claims)

        assert response["email"] == "jane@example.com"
        assert backend.get_user_details(response)["fullname"] == fullname

    def test_rejects_a_userinfo_subject_that_does_not_match_the_id_token(self) -> None:
        # The subject match is what licenses reading the remaining claims out of the ID token.
        with self.assertRaises(AuthFailed):
            self._user_data({"sub": "a-different-subject"})

    def test_rejects_an_email_domain_the_configuration_does_not_cover(self) -> None:
        with self.assertRaises(AuthFailed):
            self._user_data(ADFS_USERINFO, email="jane@unverified.example")

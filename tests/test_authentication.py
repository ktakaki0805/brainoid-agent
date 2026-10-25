"""BA-IAM-01: real SDK signature checks with local keys, identity boundaries and T-027."""

import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Any, ClassVar
from unittest.mock import patch
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
import jwt
from brainoid_api.app import create_app
from brainoid_api.auth.context import (
    AuthenticatedIdentity,
    require_recent_authentication,
    select_tenant,
)
from brainoid_api.auth.firebase_identity import FirebaseIdentityVerifier
from brainoid_api.auth.profiles import UserProfile
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends
from firebase_admin import auth
from google.auth.credentials import AnonymousCredentials
from google.auth.exceptions import TransportError
from pydantic import ValidationError
from starlette.testclient import TestClient
from test_api_foundation import CLOUD_ENV, LOCAL_ENV

UID = "Opaque user/With Spaces"
SECRET = "FIXTURE_SECRET_MUST_NOT_APPEAR"
PROFILE = {
    "user_id": UID,
    "display_name": "Fixture signed user",
    "timezone": "Asia/Tokyo",
    "status": "active",
    "version": 3,
}


class FixtureProfiles:
    def __init__(self) -> None:
        self.requests: list[FirebaseUid] = []

    def get(self, user_id: FirebaseUid) -> UserProfile:
        self.requests.append(user_id)
        return UserProfile.model_validate({**PROFILE, "user_id": user_id})


class SignedAuthenticationTests(unittest.TestCase):
    private_key: ClassVar[rsa.RSAPrivateKey]
    other_key: ClassVar[rsa.RSAPrivateKey]
    public_key: ClassVar[str]

    @classmethod
    def setUpClass(cls) -> None:
        # Ephemeral synthetic keys; never Google credentials and never persisted to disk.
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = (
            cls.private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode("ascii")
        )

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.settings = load_settings(CLOUD_ENV)
        self.app = firebase_admin.initialize_app(
            AnonymousCredentials(),  # type: ignore[no-untyped-call]
            options={"projectId": self.settings.gcp_project_id},
            name="signature-test-" + str(uuid4()),
        )
        self.addCleanup(firebase_admin.delete_app, self.app)
        self.verifier = FirebaseIdentityVerifier(self.app, self.settings)
        # Substitute ONLY network I/O. Firebase/Google SDK parsers and RSA verification run.
        self.certificates = self.enterContext(
            patch(
                "google.oauth2.id_token._fetch_certs", return_value={"fixture-key": self.public_key}
            )
        )
        self.account = self.enterContext(
            patch(
                "firebase_admin._auth_client.Client.get_user",
                return_value=auth.UserRecord(
                    {"localId": UID, "validSince": "0", "disabled": False}
                ),
            )
        )
        self.now = int(time.time())
        self.profiles = FixtureProfiles()
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            self.verifier, self.profiles
        )

        @app.post("/test/recent-authentication")
        def recent_authentication(
            identity: Annotated[AuthenticatedIdentity, Depends(authenticated_identity)],
        ) -> dict[str, bool]:
            require_recent_authentication(identity)
            return {"accepted": True}

        self.client = self.enterContext(TestClient(app))

    def token(self, changes: dict[str, Any] | None = None, *, forged: bool = False) -> str:
        claims = {
            "sub": UID,
            "aud": self.settings.gcp_project_id,
            "iss": "https://securetoken.google.com/" + self.settings.gcp_project_id,
            "iat": self.now - 5,
            "exp": self.now + 3600,
            "auth_time": self.now - 10,
            **(changes or {}),
        }
        return jwt.encode(
            claims,
            self.other_key if forged else self.private_key,
            algorithm="RS256",
            headers={"kid": "fixture-key"},
        )

    def assert_rejected(self, token: str, expected_status: int = 401) -> None:
        response = self.client.get("/v1/users/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, expected_status)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(token, response.text)
        self.assertEqual(self.profiles.requests, [])

    def test_real_signed_uid_and_public_profile_with_spoofed_body_and_claims(self) -> None:
        token = self.token({"user_id": "attacker", "role": "owner", "tenant_id": "other"})
        response = self.client.request(
            "GET",
            "/v1/users/me?user_id=attacker&role=owner",
            headers={"Authorization": "bEaReR " + token, "X-User-ID": "attacker"},
            json={"user_id": "attacker", "role": "owner", "tenant_id": "other"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), PROFILE)
        self.assertEqual(response.headers["etag"], '"3"')
        self.assertEqual(self.profiles.requests, [UID])
        self.assertTrue(self.certificates.called)
        self.assertGreaterEqual(self.account.call_count, 2)

    def test_forged_signature_is_rejected_by_sdk_cryptography(self) -> None:
        self.assert_rejected(self.token(forged=True))
        self.assertTrue(self.certificates.called)
        self.account.assert_not_called()

    def test_signed_auth_tenant_tokens_never_reach_global_profile_lookup(self) -> None:
        tenants: tuple[object, ...] = ("other-identity-tenant", "", None, False, 0, [], {})
        for tenant in tenants:
            with self.subTest(tenant=tenant):
                self.certificates.reset_mock()
                self.account.reset_mock()
                self.assert_rejected(self.token({"firebase": {"tenant": tenant}}))
                self.certificates.assert_called_once()
                # The root SDK's revocation read succeeds for the same root UID. Our
                # namespace check must still stop before the additional user/profile reads.
                self.account.assert_called_once_with(UID)

    def test_signed_malformed_firebase_metadata_is_rejected(self) -> None:
        for firebase in (None, "", "other-identity-tenant", [], ["tenant"], False, 0):
            with self.subTest(firebase=firebase):
                self.certificates.reset_mock()
                self.assert_rejected(self.token({"firebase": firebase}))
                self.certificates.assert_called_once()

    def test_signed_root_firebase_metadata_preserves_normal_authentication(self) -> None:
        for firebase in ({}, {"identities": {}, "sign_in_provider": "custom"}):
            with self.subTest(firebase=firebase):
                response = self.client.get(
                    "/v1/users/me",
                    headers={"Authorization": "Bearer " + self.token({"firebase": firebase})},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), PROFILE)

    def test_wrong_project_issuer_expired_and_bad_timestamp_claims(self) -> None:
        for changes in (
            {"aud": "another-project"},
            {"iss": "https://securetoken.google.com/another-project"},
            {"exp": self.now - 10},
            {"iat": self.now + 100},
            {"auth_time": self.now + 100},
            {"auth_time": self.now - 4},
            {"auth_time": None},
            {"auth_time": "1"},
            {"auth_time": True},
            {"auth_time": -1},
            {"auth_time": 1.5},
            {"exp": 10**50},
            {"sub": ""},
            {"sub": "x" * 129},
        ):
            with self.subTest(changes=changes):
                self.assert_rejected(self.token(changes))

    def test_unsigned_malformed_and_dedicated_device_tokens_are_not_user_tokens(self) -> None:
        for token in (
            "not-a-jwt",
            jwt.encode({"sub": UID}, key="", algorithm="none"),
            jwt.encode({"device_id": "fixture-device"}, key=SECRET * 2, algorithm="HS256"),
        ):
            with self.subTest(kind=token.split(".")[0]):
                self.assert_rejected(token)

    def test_missing_ambiguous_and_wrong_authorization_headers(self) -> None:
        token = self.token()
        for headers in (
            {},
            {"Authorization": "Basic " + token},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer  " + token},
            {"Authorization": "Bearer " + "x" * 16385},
            [("Authorization", "Bearer " + token), ("Authorization", "Bearer " + token)],
        ):
            with self.subTest(headers_count=len(headers)):
                response = self.client.get("/v1/users/me", headers=headers)
                self.assertEqual(response.status_code, 401)
        self.certificates.assert_not_called()
        self.assertEqual(self.profiles.requests, [])

    def test_revoked_disabled_deleted_and_refreshed_old_session_are_rejected(self) -> None:
        for properties in (
            {"validSince": str(self.now)},
            {"disabled": True},
            # iat passes the SDK's revocation check; auth_time must still be rejected.
            {"validSince": str(self.now - 7)},
        ):
            with self.subTest(properties=properties):
                self.account.return_value = auth.UserRecord({"localId": UID, **properties})
                self.assert_rejected(self.token())
        self.account.side_effect = auth.UserNotFoundError(SECRET)
        self.assert_rejected(self.token())

    def test_recent_authentication_uses_auth_time_and_five_minute_boundary(self) -> None:
        current = datetime.fromtimestamp(self.now, UTC)
        for age, accepted in ((299, True), (300, True), (301, False), (3600, False)):
            with self.subTest(age=age):
                identity = self.verifier.verify(self.token({"auth_time": self.now - age}))
                if accepted:
                    require_recent_authentication(identity, now=current)
                else:
                    with self.assertRaises(APIError) as caught:
                        require_recent_authentication(identity, now=current)
                    self.assertEqual(caught.exception.code, ErrorCode.REAUTH_REQUIRED)
        identity = self.verifier.verify(self.token())
        for current in (identity.authenticated_at - timedelta(seconds=1), identity.expires_at):
            with self.subTest(now=current), self.assertRaises(APIError):
                require_recent_authentication(identity, now=current)

    def test_tenant_selection_is_not_a_membership_or_authorization_context(self) -> None:
        identity = self.verifier.verify(self.token({"role": "owner", "allowed_scopes": ["*"]}))
        selection = select_tenant(identity, ResourceId("00000000-0000-0000-0000-000000000099"))
        self.assertIs(selection.identity, identity)
        self.assertNotIn("role", identity.model_dump())
        self.assertNotIn("allowed_scopes", identity.model_dump())
        self.assertNotIn("membership_status", identity.model_dump())
        self.assertNotIn("actor", selection.model_dump())
        with self.assertRaises(ValidationError):
            identity.user_id = FirebaseUid("attacker")

    def test_old_authentication_is_403_while_expired_authentication_is_401(self) -> None:
        for changes, status, code in (
            ({"auth_time": self.now - 600}, 403, "reauth_required"),
            ({"exp": self.now - 1}, 401, "authentication_required"),
        ):
            with self.subTest(status=status):
                response = self.client.post(
                    "/test/recent-authentication",
                    headers={"Authorization": "Bearer " + self.token(changes)},
                )
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertFalse(response.json()["error"]["retryable"])

    def test_sdk_network_failures_are_safe_retryable_503(self) -> None:
        self.certificates.side_effect = TransportError(SECRET)  # type: ignore[no-untyped-call]
        self.assert_rejected(self.token(), 503)

    def test_logs_and_errors_do_not_include_jwt_claims_body_or_sdk_exception(self) -> None:
        token = self.token({"aud": SECRET})
        with self.assertLogs("brainoid_api", level="DEBUG") as captured:
            self.assert_rejected(token)
            response = self.client.request(
                "GET",
                "/v1/users/me",
                headers={"Authorization": "Bearer " + token},
                json={"role": SECRET},
            )
        output = "\n".join(captured.output) + response.text
        for sensitive in (token, SECRET, UID, "Traceback", "Authorization"):
            self.assertNotIn(sensitive, output)

    def test_t027_runtime_environment_change_cannot_disable_signature_verification(self) -> None:
        for name in ("FIREBASE_AUTH_EMULATOR_HOST", "FIRESTORE_EMULATOR_HOST"):
            for value in ("127.0.0.1:9099", ""):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    self.assert_rejected(self.token(forged=True), 503)
        self.certificates.assert_not_called()

    def test_t027_explicit_settings_cannot_hide_sdk_emulator_configuration(self) -> None:
        for env in ("production", "staging"):
            settings = load_settings({**CLOUD_ENV, "APP_ENV": env})
            with patch.dict(os.environ, {"FIREBASE_AUTH_EMULATOR_HOST": "127.0.0.1:9099"}):
                with self.assertRaises(ConfigurationError):
                    FirebaseIdentityVerifier(self.app, settings)
                app = create_app(
                    settings_loader=partial(load_settings, {**CLOUD_ENV, "APP_ENV": env})
                )
                with (
                    patch("firebase_admin.initialize_app") as initialize,
                    self.assertLogs("brainoid_api"),
                    self.assertRaisesRegex(RuntimeError, "startup failed"),
                    TestClient(app),
                ):
                    self.fail("Unsafe explicit settings started the API")
                initialize.assert_not_called()


class LocalFakeAuthenticationTests(unittest.TestCase):
    def test_local_fake_never_accepts_a_dummy_bearer(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
            with TestClient(app) as client:
                self.assertEqual(client.get("/health/ready").status_code, 200)
                self.assertEqual(
                    client.get(
                        "/v1/users/me", headers={"Authorization": "Bearer fake-user"}
                    ).status_code,
                    401,
                )


if __name__ == "__main__":
    unittest.main()

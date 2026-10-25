"""Opt-in real Auth + Firestore coverage of /v1/users/me and revoked identities."""

import os
import time
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
import jwt
import yaml  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from firebase_admin import auth
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture


class AuthenticationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT, "httpTimeout": 10}, name="emulator-test-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, self.firebase)
        self.store = Client(
            project=PROJECT,
            database=DATABASE,
            credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call]
        )
        self.addCleanup(self.store.close)
        self.app = create_app()
        self.client = self.enterContext(TestClient(self.app))

    def login(self, uid: str) -> str:
        custom_token = auth.create_custom_token(uid, app=self.firebase).decode("ascii")
        response = self.emulator.request(
            "auth",
            "POST",
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
            {"token": custom_token, "returnSecureToken": True},
        )
        token: str = response["idToken"]
        return token

    def headers(self, uid: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.login(uid)}

    def test_all_fixture_users_receive_only_their_persisted_public_profile(self) -> None:
        specification = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        validator = Draft202012Validator(
            {"$ref": "#/components/schemas/User", "components": specification["components"]}
        )
        for user in load_fixture()["auth_users"]:
            uid = user["localId"]
            with self.subTest(uid=uid):
                response = self.client.request(
                    "GET",
                    "/v1/users/me?user_id=demo-b-owner&role=owner",
                    headers={**self.headers(uid), "X-User-ID": "demo-b-owner"},
                    json={"user_id": "demo-b-owner", "role": "owner"},
                )
                self.assertEqual(response.status_code, 200)
                validator.validate(response.json())
                self.assertEqual(
                    response.json(),
                    {
                        "user_id": uid,
                        "display_name": user["displayName"],
                        "timezone": "Asia/Tokyo",
                        "status": "active",
                        "version": 1,
                    },
                )
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["etag"], '"1"')
        self.assertEqual(self.client.get("/users/me").status_code, 404)

    def test_revoked_and_disabled_tokens_return_401(self) -> None:
        for uid, action in (("demo-a-member", "revoke"), ("demo-b-member", "disable")):
            with self.subTest(action=action):
                headers = self.headers(uid)
                self.assertEqual(self.client.get("/v1/users/me", headers=headers).status_code, 200)
                if action == "revoke":
                    # Firebase timestamps have second precision; revoke strictly after auth_time.
                    time.sleep(1.1)
                    auth.revoke_refresh_tokens(uid, app=self.firebase)
                else:
                    auth.update_user(uid, disabled=True, app=self.firebase)
                response = self.client.get("/v1/users/me", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "authentication_required")

    def test_emulator_tokens_still_require_project_issuer_and_valid_times(self) -> None:
        token = self.login("demo-a-owner")
        claims = jwt.decode(token, options={"verify_signature": False})
        now = int(time.time())
        for change in (
            {"aud": "demo-other-project"},
            {"iss": "https://securetoken.google.com/demo-other-project"},
            {"exp": now - 1},
            {"iat": now + 60},
            {"auth_time": now + 60},
            {"auth_time": None},
        ):
            with self.subTest(change=change):
                changed = jwt.encode({**claims, **change}, key="", algorithm="none")
                response = self.client.get(
                    "/v1/users/me", headers={"Authorization": "Bearer " + changed}
                )
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/v1/users/me").status_code, 401)

    def test_missing_profile_is_404_without_automatic_provisioning(self) -> None:
        uid = "fixture-new-user"
        response = self.client.get("/v1/users/me", headers=self.headers(uid))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.store.collection("users").document(uid).get().exists)

    def test_opaque_uid_cannot_be_interpreted_as_a_firestore_path(self) -> None:
        uid = "Opaque / User % With Spaces"
        profile = {
            "user_id": uid,
            "display_name": "FIXTURE_OPAQUE_USER",
            "timezone": "UTC",
            "status": "active",
            "version": 7,
        }
        self.store.collection("users").document("safe-fixture-key").set(profile)
        response = self.client.get("/v1/users/me", headers=self.headers(uid))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), profile)

    def test_duplicate_corrupt_or_deleting_profile_fails_closed(self) -> None:
        uid = "demo-a-owner"
        reference = self.store.collection("users").document(uid)
        original = reference.get().to_dict()
        assert original is not None
        headers = self.headers(uid)
        changes: list[dict[str, Any]] = [
            {"timezone": "invalid/timezone"},
            {"version": True},
            {"display_name": ""},
            {"status": "unknown"},
        ]
        for change in changes:
            with self.subTest(change=change):
                reference.set({**original, **change})
                with self.assertLogs("brainoid_api") as logs:
                    response = self.client.get("/v1/users/me", headers=headers)
                self.assertEqual(response.status_code, 500)
                self.assertNotIn(uid, "\n".join(logs.output))
        reference.set({**original, "status": "deleting"})
        self.assertEqual(self.client.get("/v1/users/me", headers=headers).status_code, 403)
        reference.set(original)
        self.store.collection("users").document("duplicate-fixture-key").set(original)
        with self.assertLogs("brainoid_api"):
            self.assertEqual(self.client.get("/v1/users/me", headers=headers).status_code, 500)


if __name__ == "__main__":
    unittest.main()

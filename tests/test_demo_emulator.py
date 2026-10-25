"""Local demo safety and fixture contracts; no running emulator is required."""

import contextlib
import io
import json
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from brainoid_contracts import ResourceId, Scope

from scripts.demo_emulator import HOSTS, ROOT, DemoEmulator, DemoError, load_fixture, main

DEMO_ENV = {
    "APP_ENV": "local-emulator",
    "AUTH_PROVIDER": "firebase",
    "MODEL_PROVIDER": "fake",
    "GCP_PROJECT_ID": "demo-brainoid",
    "GCP_REGION": "asia-northeast1",
    "FIRESTORE_DATABASE_ID": "(default)",
    "MODEL_ID": "fake-model",
    "FIRESTORE_EMULATOR_HOST": HOSTS["firestore"],
    "FIREBASE_AUTH_EMULATOR_HOST": HOSTS["auth"],
    "AUTHZ_LEDGER_DIRECTORY": "/tmp/brainoid-test-authz-ledger",
    "CURSOR_SIGNING_KEY": "synthetic-cursor-signing-key-for-tests-only",
}


class DemoSafetyTests(unittest.TestCase):
    def test_unsafe_configuration_fails_before_any_io_for_all_commands(self) -> None:
        changes = [
            {"APP_ENV": "production"},
            {"APP_ENV": "staging"},
            {"APP_ENV": "local-fake"},
            {"GCP_PROJECT_ID": "production-project"},
            {"GCP_PROJECT_ID": "demo-another"},
            {"GOOGLE_CLOUD_PROJECT": "production-project"},
            {"GCLOUD_PROJECT": "production-project"},
            {"MODEL_PROVIDER": "vertex"},
            {"MODEL_ID": "real-model"},
            {"AUTH_PROVIDER": "fake"},
            {"FIRESTORE_DATABASE_ID": "other-database"},
            {"SPEECH_PROVIDER": "real"},
            {"HOME_ASSISTANT_PROVIDER": "real"},
            {"FIRESTORE_EMULATOR_HOST": "firestore.googleapis.com:443"},
            {"FIRESTORE_EMULATOR_HOST": "127.0.0.1:8081"},
            {"FIREBASE_AUTH_EMULATOR_HOST": "example.invalid:9099"},
            {"FIREBASE_AUTH_EMULATOR_HOST": "http://127.0.0.1:9099"},
        ]
        with patch("http.client.HTTPConnection") as connection, patch("os.execvpe") as execute:
            for command in ("seed", "reset", "start", "test"):
                for changed in changes:
                    with self.subTest(command=command, fields=tuple(changed)):
                        stderr = io.StringIO()
                        with contextlib.redirect_stderr(stderr):
                            self.assertEqual(main([command], {**DEMO_ENV, **changed}), 1)
                        self.assertNotIn(next(iter(changed.values())), stderr.getvalue())
                for name in DEMO_ENV:
                    incomplete = {k: v for k, v in DEMO_ENV.items() if k != name}
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(main([command], incomplete), 1)
            connection.assert_not_called()
            execute.assert_not_called()

    def test_transport_never_follows_redirects_or_exposes_body(self) -> None:
        with (
            patch.dict("os.environ", {"HTTP_PROXY": "http://example.invalid:8888", "NO_PROXY": ""}),
            patch("http.client.HTTPConnection") as factory,
        ):
            connection = factory.return_value
            connection.getresponse.return_value.status = 307
            connection.getresponse.return_value.read.return_value = b"secret-response"
            with self.assertRaisesRegex(DemoError, r"HTTP 307"):
                DemoEmulator(DEMO_ENV).users()
            factory.assert_called_once_with(HOSTS["auth"], timeout=10)
            connection.request.assert_called_once()
            connection.getresponse.return_value.read.assert_not_called()
            connection.close.assert_called_once()

    def test_fixture_covers_two_tenants_roles_scopes_devices_and_fixed_times(self) -> None:
        fixture = load_fixture()
        users = {user["localId"] for user in fixture["auth_users"]}
        self.assertEqual(len(users), 6)
        docs = {document["path"]: document["fields"] for document in fixture["documents"]}
        self.assertEqual(len(docs), len(fixture["documents"]))
        tenants = {path.split("/")[1] for path in docs if path.startswith("tenants/")}
        self.assertEqual(len(tenants), 2)
        timestamp = datetime(2026, 10, 25, 10, tzinfo=UTC)
        for user in fixture["auth_users"]:
            self.assertEqual(user["createdAt"], str(int(timestamp.timestamp() * 1000)))
            self.assertEqual(user["lastLoginAt"], user["createdAt"])
            self.assertEqual(set(user), {"localId", "displayName", "createdAt", "lastLoginAt"})
        for tenant in tenants:
            self.assertEqual(ResourceId(tenant), tenant)
            scoped = {
                path: fields
                for path, fields in docs.items()
                if path.startswith(f"tenants/{tenant}/")
            }
            self.assertEqual(
                {v["role"] for p, v in scoped.items() if "/memberships/" in p},
                {"owner", "admin", "member"},
            )
            self.assertEqual(
                {v["status"] for p, v in scoped.items() if "/devices/" in p}, {"active", "revoked"}
            )
            for collection in ("notes", "conversations"):
                self.assertEqual(
                    {v["visibility"] for p, v in scoped.items() if p.split("/")[-2] == collection},
                    {"private", "shared"},
                )
            for fields in scoped.values():
                self.assertEqual(fields["tenant_id"], tenant)
                if "visibility" in fields:
                    Scope(visibility=fields["visibility"], display_policy=fields["display_policy"])
                    self.assertIn(fields["owner_user_id"], users)
                    if fields["visibility"] == "private":
                        self.assertEqual(
                            docs[f"tenants/{tenant}/memberships/" + fields["owner_user_id"]][
                                "role"
                            ],
                            "member",
                        )
        for fields in docs.values():
            self.assertEqual(fields["created_at"], timestamp.isoformat().replace("+00:00", "Z"))
            self.assertEqual(fields["updated_at"], fields["created_at"])
            if "id" in fields:
                self.assertEqual(ResourceId(fields["id"]), fields["id"])
            for name in ("text", "body", "title"):
                if name in fields:
                    self.assertTrue(fields[name].startswith("FIXTURE_"))
            self.assertFalse({"token", "password", "credential", "email"} & fields.keys())

    def test_emulator_configuration_binds_only_loopback(self) -> None:
        config = json.loads((ROOT / "firebase.json").read_text())
        for service in ("auth", "firestore", "hub", "logging"):
            self.assertEqual(config["emulators"][service]["host"], "127.0.0.1")
        for service, host in HOSTS.items():
            self.assertEqual(str(config["emulators"][service]["port"]), host.split(":")[1])
        self.assertFalse(config["emulators"]["ui"]["enabled"])


if __name__ == "__main__":
    unittest.main()

"""Opt-in tests against real Auth/Firestore emulators; run via test-emulator."""

import http.client
import json
import os
import subprocess
import unittest
from collections import defaultdict
from typing import Any

from scripts.demo_emulator import ROOT, DemoEmulator, load_fixture


def decode_value(value: dict[str, Any]) -> Any:
    if "arrayValue" in value:
        return [decode_value(item) for item in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {k: decode_value(v) for k, v in value["mapValue"].get("fields", {}).items()}
    if "integerValue" in value:
        return int(value["integerValue"])
    return next(iter(value.values()))


class SeedIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)

    def snapshot(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        fixture = load_fixture()
        expected_by_collection: dict[str, dict[str, Any]] = defaultdict(dict)
        for document in fixture["documents"]:
            name = self.emulator.document_prefix + "/" + document["path"]
            expected_by_collection[document["path"].rsplit("/", 1)[0]][name] = document["fields"]
        actual = {}
        for collection, expected in expected_by_collection.items():
            result = self.emulator.request(
                "firestore", "GET", "/v1/" + self.emulator.document_prefix + "/" + collection
            )
            self.assertNotIn("nextPageToken", result)
            documents = {
                doc["name"]: {k: decode_value(v) for k, v in doc["fields"].items()}
                for doc in result.get("documents", [])
            }
            self.assertEqual(documents, expected)
            actual.update(documents)
        users = sorted(self.emulator.users(), key=lambda user: user["localId"])
        self.assertEqual(
            {u["localId"] for u in users}, {u["localId"] for u in fixture["auth_users"]}
        )
        for expected in fixture["auth_users"]:
            user = next(user for user in users if user["localId"] == expected["localId"])
            for key, value in expected.items():
                self.assertEqual(user[key], value)
        return users, actual

    def test_seed_twice_reset_and_seed_again(self) -> None:
        subprocess.run([str(ROOT / "scripts/seed_demo")], cwd=ROOT, check=True)
        first = self.snapshot()
        subprocess.run([str(ROOT / "scripts/seed_demo")], cwd=ROOT, check=True)
        self.assertEqual(self.snapshot(), first)

        # The emulator-only clear must also remove nested messages and test-created records.
        self.emulator.request(
            "firestore",
            "PATCH",
            "/v1/" + self.emulator.document_prefix + "/extra/test/nested/child",
            {"fields": {"x": {"integerValue": "1"}}},
        )
        self.emulator.reset()
        self.assertEqual(self.emulator.users(), [])
        collections = {d["path"].rsplit("/", 1)[0] for d in load_fixture()["documents"]}
        for collection in collections | {"extra/test/nested"}:
            result = self.emulator.request(
                "firestore", "GET", "/v1/" + self.emulator.document_prefix + "/" + collection
            )
            self.assertEqual(result.get("documents", []), [])
        self.emulator.seed()
        self.assertEqual(self.snapshot()[1], first[1])

    def test_unsafe_seed_cli_leaves_real_emulators_unchanged(self) -> None:
        self.emulator.seed()
        before = self.snapshot()
        for overrides in ({"GCP_PROJECT_ID": "production-project"}, {"APP_ENV": "production"}):
            for command in ("seed", "reset"):
                result = subprocess.run(
                    [str(ROOT / "scripts/dev_emulator"), command],
                    cwd=ROOT,
                    env={**os.environ, **overrides},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_direct_unauthenticated_firestore_clients_are_denied(self) -> None:
        self.emulator.seed()
        connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=10)
        self.addCleanup(connection.close)
        path = "/v1/" + self.emulator.document_prefix + "/" + load_fixture()["documents"][0]["path"]
        connection.request("GET", path)
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.read())["error"]["status"], "PERMISSION_DENIED")


if __name__ == "__main__":
    unittest.main()

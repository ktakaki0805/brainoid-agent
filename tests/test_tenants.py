"""IAM-03 HTTP contracts and current-authority bootstrap/list/update boundaries."""

import copy
import json
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.tenants import TenantCreate, Tenants
from brainoid_api.settings import ConfigurationError, load_settings
from brainoid_contracts import FirebaseUid, ResourceId
from fakes.runtime import FakeClock
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from starlette.testclient import TestClient
from test_api_foundation import CLOUD_ENV, LOCAL_ENV
from test_common_mutations import MemoryStore

KEY = "synthetic-tenant-key-001"


class TenantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.store = MemoryStore()
        fixture = json.loads(Path("tests/fixtures/tenants/demo.json").read_text())
        self.store.data = {doc["path"]: doc["fields"] for doc in fixture["documents"]}
        self.a, self.b = [
            ResourceId(path.split("/")[1])
            for path in self.store.data
            if path.startswith("tenants/") and path.count("/") == 1
        ]
        self.repo = Tenants(self.store, Cursors(b"x" * 32, now=self.clock.now), now=self.clock.now)
        self.uid = "demo-a-owner"
        app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(tenants=self.repo)
        self.client = self.enterContext(TestClient(app))
        self.spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())

    def identity(self, uid: str | None = None) -> AuthenticatedIdentity:
        now = self.clock.now()
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid or self.uid),
            authenticated_at=now,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )

    def validate(self, schema: str, value: Any) -> None:
        Draft202012Validator(
            {"$ref": "#/components/schemas/" + schema, "components": self.spec["components"]}
        ).validate(json.loads(json.dumps(value, default=lambda v: v.isoformat())))

    def create(self, key: str = KEY, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(
            "/v1/tenants", json=body or {"name": "Synthetic home"}, headers={"Idempotency-Key": key}
        )

    def update(
        self,
        tenant: ResourceId | None = None,
        *,
        key: str = KEY,
        version: str = '"1"',
        body: dict[str, Any] | None = None,
    ) -> Any:
        return self.client.patch(
            f"/v1/tenants/{tenant or self.a}",
            json=body or {"name": "Updated home"},
            headers={"Idempotency-Key": key, "If-Match": version},
        )

    def test_bootstrap_retry_saves_exactly_one_owner_and_initial_agent(self) -> None:
        self.store.retry_once = True
        first = self.create()
        self.assertEqual(first.status_code, 201)
        self.validate("Tenant", first.json())
        second = self.create(body={"timezone": "Asia/Tokyo", "name": "Synthetic home"})
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second.headers["etag"], '"1"')
        tenant = first.json()["id"]
        documents = {p: d for p, d in self.store.data.items() if p.startswith(f"tenants/{tenant}/")}
        self.assertEqual(len(documents), 2)
        member = next(d for p, d in documents.items() if "/memberships/" in p)
        self.assertEqual((member["user_id"], member["role"]), (self.uid, "owner"))
        agent = next(d for p, d in documents.items() if "/agents/" in p)
        self.validate(
            "Agent",
            {k: agent[k] for k in self.spec["components"]["schemas"]["Agent"]["properties"]},
        )
        self.assertEqual(
            (agent["name"], agent["persona"], agent["allowed_tools"]), ("Brainoid", "", [])
        )
        self.assertEqual(self.create(body={"name": "Other"}).status_code, 409)

    def test_unprovisioned_opaque_user_can_create_without_profile_or_path_injection(self) -> None:
        self.uid = "Opaque / User % Name"
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertFalse(
            any(
                d.get("user_id") == self.uid
                for p, d in self.store.data.items()
                if p.startswith("users/")
            )
        )
        member_paths = [p for p, d in self.store.data.items() if d.get("user_id") == self.uid]
        self.assertEqual(len(member_paths), 1)
        self.assertEqual(len(member_paths[0].split("/")), 4)
        self.assertEqual(
            [t["id"] for t in self.client.get("/v1/tenants").json()["items"]],
            [response.json()["id"]],
        )

    def test_account_deleting_and_duplicate_profile_block_bootstrap_and_reads(self) -> None:
        self.create()
        self.store.data[f"users/{self.uid}"]["status"] = "deleting"
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.client.get("/v1/tenants").status_code, 403)
        self.assertEqual(self.client.get(f"/v1/tenants/{self.a}").status_code, 403)
        self.store.data[f"users/{self.uid}"]["status"] = "active"
        self.store.data["users/duplicate"] = copy.deepcopy(self.store.data[f"users/{self.uid}"])
        self.assertEqual(self.create().status_code, 500)

    def test_t001_list_get_and_patch_do_not_expose_other_tenant(self) -> None:
        response = self.client.get("/v1/tenants")
        self.validate("TenantPage", response.json())
        self.assertEqual([t["id"] for t in response.json()["items"]], [self.a])
        self.assertEqual(self.client.get(f"/v1/tenants/{self.b}").status_code, 404)
        self.assertEqual(self.update(self.b).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/tenants/{self.a}").json()["my_role"], "owner")
        self.assertEqual(self.client.get("/tenants").status_code, 404)

    def test_only_admin_owner_can_update_and_replays_return_current_resource(self) -> None:
        self.uid = "demo-a-member"
        self.assertEqual(self.update().status_code, 403)
        self.uid = "demo-a-admin"
        first = self.update()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["etag"], '"2"')
        self.validate("Tenant", first.json())
        self.assertEqual(self.update().json(), first.json())
        self.assertEqual(self.update(key=KEY + "-stale").status_code, 412)
        self.assertEqual(self.update(version='"2"').status_code, 409)
        latest = self.update(key=KEY + "-next", version='"2"', body={"timezone": "UTC"})
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(self.update().json(), latest.json())
        self.assertEqual(latest.headers["etag"], '"3"')

    def test_revocation_blocks_selection_and_replays(self) -> None:
        created = self.create().json()["id"]
        self.update()
        for path, document in self.store.data.items():
            if "/memberships/" in path and document["user_id"] == self.uid:
                document.update(status="revoked", authorization_version=2)
        self.assertEqual(self.create().status_code, 404)
        self.assertEqual(self.update().status_code, 404)
        self.assertEqual(self.client.get(f"/v1/tenants/{created}").status_code, 404)
        self.assertEqual(self.client.get("/v1/tenants").json()["items"], [])

    def test_role_change_invalidates_update_and_bootstrap_replay(self) -> None:
        created = self.create().json()["id"]
        self.update()
        for path, document in self.store.data.items():
            if "/memberships/" in path and document["user_id"] == self.uid:
                document.update(role="admin", authorization_version=2)
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.update().status_code, 403)
        self.assertEqual(self.client.get(f"/v1/tenants/{created}").json()["my_role"], "admin")

    def test_cursor_binds_user_limit_and_entire_current_membership_set(self) -> None:
        self.create()
        page = self.client.get("/v1/tenants?limit=1").json()
        cursor = page["next_cursor"]
        self.assertIsInstance(cursor, str)
        next_page = self.client.get("/v1/tenants", params={"limit": 1, "cursor": cursor})
        self.assertEqual(next_page.status_code, 200)
        self.assertNotEqual(page["items"][0]["id"], next_page.json()["items"][0]["id"])
        self.assertIsNone(next_page.json()["next_cursor"])
        self.assertEqual(
            self.client.get("/v1/tenants", params={"limit": 2, "cursor": cursor}).status_code, 400
        )
        self.uid = "demo-b-owner"
        self.assertEqual(
            self.client.get("/v1/tenants", params={"limit": 1, "cursor": cursor}).status_code, 400
        )
        self.uid = "demo-a-owner"
        self.store.data[f"tenants/{self.a}/memberships/{self.uid}"]["status"] = "revoked"
        self.assertEqual(
            self.client.get("/v1/tenants", params={"limit": 1, "cursor": cursor}).status_code, 400
        )

    def test_invalid_contract_fields_headers_and_body_are_common_errors(self) -> None:
        for body in (
            {"name": ""},
            {"name": "x", "role": "owner"},
            {"name": "x", "timezone": "Bad/Zone"},
        ):
            self.assertEqual(self.create(body=body).status_code, 422)
        for invalid_patch in ({}, {"name": None}, {"my_role": "owner"}, {"timezone": None}):
            response = self.client.patch(
                f"/v1/tenants/{self.a}",
                json=invalid_patch,
                headers={"Idempotency-Key": KEY, "If-Match": '"1"'},
            )
            self.assertEqual(response.status_code, 422)
            self.validate("Error", response.json())
        self.assertEqual(self.client.post("/v1/tenants", json={"name": "x"}).status_code, 400)
        self.assertEqual(
            self.client.patch(
                f"/v1/tenants/{self.a}", json={"name": "x"}, headers={"Idempotency-Key": KEY}
            ).status_code,
            428,
        )
        self.assertEqual(self.update(version='W/"1"').status_code, 400)
        self.assertEqual(self.client.get("/v1/tenants?limit=101").status_code, 422)
        self.assertEqual(
            self.client.post(
                "/v1/tenants",
                json={"name": "x"},
                headers=[("Idempotency-Key", KEY), ("Idempotency-Key", KEY)],
            ).status_code,
            400,
        )

    def test_identity_expiry_blocks_database_access(self) -> None:
        identity = self.identity()
        self.clock.advance(3600)
        with self.assertRaises(APIError) as error:
            self.repo.create(identity, TenantCreate(name="x"), KEY)
        self.assertEqual(error.exception.code, ErrorCode.AUTHENTICATION_REQUIRED)

    def test_bootstrap_24_hour_expiry_starts_a_new_operation(self) -> None:
        first = self.create().json()["id"]
        self.clock.advance(86399)
        self.assertEqual(self.create().json()["id"], first)
        self.clock.advance(1)
        self.assertNotEqual(self.create().json()["id"], first)

    def test_cursor_expiry_and_tampering_are_rejected(self) -> None:
        self.create()
        cursor = self.client.get("/v1/tenants?limit=1").json()["next_cursor"]
        self.assertEqual(
            self.client.get("/v1/tenants", params={"limit": 1, "cursor": cursor + "x"}).status_code,
            400,
        )
        self.clock.advance(3600)
        self.assertEqual(
            self.client.get("/v1/tenants", params={"limit": 1, "cursor": cursor}).status_code, 400
        )

    def test_duplicate_membership_cannot_provide_authority(self) -> None:
        document = copy.deepcopy(self.store.data[f"tenants/{self.a}/memberships/{self.uid}"])
        self.store.data[f"tenants/{self.a}/memberships/duplicate"] = document
        self.assertEqual(self.client.get(f"/v1/tenants/{self.a}").status_code, 404)
        self.assertEqual(self.update().status_code, 404)
        self.assertEqual(self.client.get("/v1/tenants").json()["items"], [])

    def test_repository_calls_run_outside_event_loop(self) -> None:
        caller_threads = []
        original = self.repo.list

        def capture(*args: Any, **kwargs: Any) -> Any:
            caller_threads.append(threading.current_thread().name)
            return original(*args, **kwargs)

        with patch.object(self.repo, "list", side_effect=capture):
            self.assertEqual(self.client.get("/v1/tenants").status_code, 200)
        self.assertTrue(all("AnyIO worker thread" in name for name in caller_threads))

    def test_cursor_key_is_explicit_and_redacted(self) -> None:
        secret = CLOUD_ENV["CURSOR_SIGNING_KEY"]
        settings = load_settings(CLOUD_ENV)
        self.assertNotIn(secret, repr(settings))
        self.assertNotIn(secret, settings.model_dump_json())
        for key in (None, "short"):
            env = {k: v for k, v in CLOUD_ENV.items() if k != "CURSOR_SIGNING_KEY"}
            if key is not None:
                env["CURSOR_SIGNING_KEY"] = key
            with self.assertRaises(ConfigurationError) as error:
                load_settings(env)
            self.assertNotIn(secret, str(error.exception))

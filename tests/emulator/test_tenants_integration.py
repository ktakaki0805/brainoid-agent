"""IAM-03 real Firebase/Firestore HTTP, concurrent bootstrap, rollback and lost commits."""

import os
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import firebase_admin  # type: ignore[import-untyped]
from brainoid_api.app import create_app
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.firestore import FirestoreTransactions, _Transaction
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.tenants import Tenant, TenantCreate, TenantPatch, Tenants
from brainoid_contracts import FirebaseUid, ResourceId
from firebase_admin import auth
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from starlette.testclient import TestClient

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture

KEY = "synthetic-tenant-key-001"


class TenantIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.firebase = firebase_admin.initialize_app(
            options={"projectId": PROJECT, "httpTimeout": 10}, name="tenant-test-" + str(uuid4())
        )
        self.addCleanup(firebase_admin.delete_app, self.firebase)
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        self.now = datetime.now(UTC)
        self.repo = Tenants(
            FirestoreTransactions(self.db),
            Cursors(b"x" * 32, now=lambda: self.now),
            now=lambda: self.now,
        )
        self.a, self.b = [
            ResourceId(d["fields"]["tenant_id"])
            for d in load_fixture()["documents"]
            if d["path"].startswith("tenants/") and d["path"].count("/") == 1
        ]
        self.http = self.enterContext(TestClient(create_app()))

    def identity(self, uid: str = "demo-a-owner") -> AuthenticatedIdentity:
        return AuthenticatedIdentity(
            user_id=FirebaseUid(uid),
            authenticated_at=self.now,
            issued_at=self.now,
            expires_at=self.now + timedelta(hours=1),
        )

    def headers(self, uid: str = "demo-a-owner") -> dict[str, str]:
        custom = auth.create_custom_token(uid, app=self.firebase).decode("ascii")
        response = self.emulator.request(
            "auth",
            "POST",
            "/identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=fixture-key",
            {"token": custom, "returnSecureToken": True},
        )
        return {"Authorization": "Bearer " + response["idToken"], "Idempotency-Key": KEY}

    def create(self, *, name: str = "Synthetic home", uid: str = "demo-a-owner") -> Tenant:
        return self.repo.create(self.identity(uid), TenantCreate(name=name), KEY)

    def concurrent(
        self, first: Callable[[], Tenant], second: Callable[[], Tenant]
    ) -> list[Tenant | ErrorCode]:
        barrier = Barrier(2)

        def invoke(call: Callable[[], Tenant]) -> Tenant | ErrorCode:
            barrier.wait(timeout=10)
            try:
                return call()
            except APIError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(invoke, call) for call in (first, second)]
            return [result.result(timeout=30) for result in results]

    def test_concurrent_bootstrap_has_one_tenant_owner_agent_and_receipt(self) -> None:
        results = self.concurrent(self.create, self.create)
        self.assertIsInstance(results[0], Tenant)
        self.assertEqual(results[0], results[1])
        result = results[0]
        assert isinstance(result, Tenant)
        self.assertEqual(len(list(self.db.collection("tenants").stream())), 3)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 1)
        self.assertEqual(len(list(self.db.collection(f"tenants/{result.id}/agents").stream())), 1)
        owners = list(self.db.collection(f"tenants/{result.id}/memberships").stream())
        self.assertEqual(len(owners), 1)
        self.assertEqual(
            (owners[0].get("role"), owners[0].get("user_id")), ("owner", "demo-a-owner")
        )
        other = self.create(uid="demo-b-owner")
        self.assertNotEqual(other.id, result.id)

    def test_concurrent_body_conflict_only_commits_one_bootstrap(self) -> None:
        results = self.concurrent(self.create, lambda: self.create(name="Other home"))
        self.assertEqual(sum(isinstance(r, Tenant) for r in results), 1)
        self.assertIn(ErrorCode.IDEMPOTENCY_CONFLICT, results)
        self.assertEqual(len(list(self.db.collection("tenants").stream())), 3)

    def test_mid_bootstrap_failure_rolls_back_tenant_owner_agent_and_receipt(self) -> None:
        original = _Transaction.create

        def fail(transaction: _Transaction, path: str, document: dict[str, Any]) -> None:
            original(transaction, path, document)
            if "/agents/" in path:
                raise RuntimeError("synthetic failure after all business writes are staged")

        with patch.object(_Transaction, "create", fail), self.assertRaises(RuntimeError):
            self.create()
        self.assertEqual(len(list(self.db.collection("tenants").stream())), 2)
        self.assertEqual(len(list(self.db.collection_group("memberships").stream())), 6)
        self.assertEqual(len(list(self.db.collection_group("agents").stream())), 2)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 0)
        self.assertEqual(self.create().my_role, "owner")

    def lose_commit(self, work: Callable[[], Tenant]) -> None:
        commit = self.db._firestore_api.commit

        def lose_response(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose_response):
            with self.assertRaises(APIError) as error:
                work()
        self.assertEqual(error.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)

    def test_lost_create_and_update_commit_responses_replay_without_duplicates(self) -> None:
        self.lose_commit(self.create)
        self.repo = Tenants(
            FirestoreTransactions(self.db),
            Cursors(b"x" * 32, now=lambda: self.now),
            now=lambda: self.now,
        )
        tenant = self.create()
        self.assertEqual(len(list(self.db.collection("tenants").stream())), 3)
        self.assertEqual(len(list(self.db.collection(f"tenants/{tenant.id}/agents").stream())), 1)

        def update() -> Tenant:
            return self.repo.update(
                self.identity(), tenant.id, TenantPatch(name="Updated"), KEY, '"1"'
            )

        self.lose_commit(update)
        replay = update()
        self.assertEqual((replay.name, replay.version), ("Updated", 2))
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 2)

    def test_t001_real_auth_http_list_settings_and_current_selection(self) -> None:
        for headers in ({}, {"Authorization": "Bearer synthetic-device-token"}):
            self.assertEqual(self.http.get("/v1/tenants", headers=headers).status_code, 401)
            self.assertEqual(
                self.http.post("/v1/tenants", json={"name": "x"}, headers=headers).status_code, 401
            )
        for uid, tenant, other in (
            ("demo-a-owner", self.a, self.b),
            ("demo-b-owner", self.b, self.a),
        ):
            headers = self.headers(uid)
            listing = self.http.get("/v1/tenants", headers=headers)
            self.assertEqual(listing.status_code, 200)
            self.assertEqual([item["id"] for item in listing.json()["items"]], [tenant])
            self.assertEqual(
                self.http.get(f"/v1/tenants/{other}", headers=headers).status_code, 404
            )
            update_headers = {**headers, "If-Match": '"1"'}
            self.assertEqual(
                self.http.patch(
                    f"/v1/tenants/{other}", json={"name": "attack"}, headers=update_headers
                ).status_code,
                404,
            )
            changed = self.http.patch(
                f"/v1/tenants/{tenant}", json={"timezone": "UTC"}, headers=update_headers
            )
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(changed.headers["etag"], '"2"')
            self.assertEqual(
                self.http.patch(
                    f"/v1/tenants/{tenant}", json={"timezone": "UTC"}, headers=update_headers
                ).json(),
                changed.json(),
            )
            self.db.document(f"tenants/{tenant}/memberships/{uid}").update(
                {"status": "revoked", "authorization_version": 2}
            )
            self.assertEqual(
                self.http.get(f"/v1/tenants/{tenant}", headers=headers).status_code, 404
            )
            self.assertEqual(
                self.http.patch(
                    f"/v1/tenants/{tenant}", json={"timezone": "UTC"}, headers=update_headers
                ).status_code,
                404,
            )
            self.assertEqual(self.http.get("/v1/tenants", headers=headers).json()["items"], [])

    def test_competing_owner_admin_updates_enforce_version_in_real_transaction(self) -> None:
        def update(uid: str, key: str) -> Tenant:
            return self.repo.update(self.identity(uid), self.a, TenantPatch(name=uid), key, '"1"')

        results = self.concurrent(
            lambda: update("demo-a-owner", KEY), lambda: update("demo-a-admin", KEY + "2")
        )
        self.assertEqual(sum(isinstance(r, Tenant) for r in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        self.assertEqual(self.repo.get(self.identity(), self.a).version, 2)

    def test_opaque_unprovisioned_user_creation_and_profile_contract(self) -> None:
        uid = "Opaque / New User % Name"
        headers = self.headers(uid)
        self.assertEqual(self.http.get("/v1/users/me", headers=headers).status_code, 404)
        first = self.http.post("/v1/tenants", json={"name": "New home"}, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(
            self.http.post("/v1/tenants", json={"name": "New home"}, headers=headers).json(),
            first.json(),
        )
        self.assertEqual(self.http.get("/v1/users/me", headers=headers).status_code, 404)
        self.assertEqual(
            self.http.get("/v1/tenants", headers=headers).json()["items"], [first.json()]
        )
        members = list(self.db.collection(f"tenants/{first.json()['id']}/memberships").stream())
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].get("user_id"), uid)
        members[0].reference.update({"role": "admin", "authorization_version": 2})
        self.assertEqual(
            self.http.post("/v1/tenants", json={"name": "New home"}, headers=headers).status_code,
            403,
        )

    def test_cursor_rechecks_collection_group_and_existing_deleting_user(self) -> None:
        self.create()
        headers = self.headers()
        page = self.http.get("/v1/tenants?limit=1", headers=headers).json()
        cursor = page["next_cursor"]
        self.assertIsNotNone(cursor)
        self.assertEqual(
            self.http.get(
                "/v1/tenants", params={"limit": 1, "cursor": cursor}, headers=headers
            ).status_code,
            200,
        )
        self.db.document(f"tenants/{self.a}/memberships/demo-a-owner").update({"status": "revoked"})
        self.assertEqual(
            self.http.get(
                "/v1/tenants", params={"limit": 1, "cursor": cursor}, headers=headers
            ).status_code,
            400,
        )
        self.db.document("users/demo-a-owner").update({"status": "deleting"})
        self.assertEqual(
            self.http.post(
                "/v1/tenants", json={"name": "Synthetic home"}, headers=headers
            ).status_code,
            403,
        )
        self.assertEqual(self.http.get("/v1/tenants", headers=headers).status_code, 403)

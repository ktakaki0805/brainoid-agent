"""Real Firestore transactions; only the validated, disposable demo emulator is used."""

import os
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.common.access import AuthorizedScope
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.idempotency import Authorize, Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors
from brainoid_api.common.transactions import Transaction
from brainoid_api.common.versioning import compare_and_write
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    Visibility,
)
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture

KEY = "emulator-fixed-key-001"


class CommonMutationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Validate all settings before constructing the SDK or touching any socket.
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        credentials = AnonymousCredentials()  # type: ignore[no-untyped-call]
        self.client = Client(project=PROJECT, database=DATABASE, credentials=credentials)
        self.addCleanup(self.client.close)
        self.store = FirestoreTransactions(self.client)
        self.time = datetime(2026, 10, 26, tzinfo=UTC)
        self.idempotency = Idempotency(self.store, now=lambda: self.time)
        self.tenant = ResourceId(load_fixture()["documents"][0]["fields"]["tenant_id"])
        self.collection = f"tenants/{self.tenant}/common_mutation_tests"

    def authorizer(self, user: str = "demo-a-owner", path: str | None = None) -> Authorize:
        """Test policy only; real IAM is a later ticket. Every check reads this transaction."""

        def authorize(transaction: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            tenant = transaction.get(f"tenants/{self.tenant}")
            member = transaction.get(f"tenants/{self.tenant}/memberships/{user}")
            if (
                tenant is None
                or tenant["status"] != "active"
                or member is None
                or member["status"] != "active"
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            owner = FirebaseUid("demo-a-owner")
            visibility = "shared"
            target = path
            if replay is not None:
                target = self.collection + "/" + str(replay.resource_id)
            if target is not None:
                resource = transaction.get(target)
                if resource is None:
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                owner = FirebaseUid(resource["created_by"])
                visibility = resource["visibility"]
                if visibility == "private" and user != owner:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                if user != owner and member["role"] not in ("admin", "owner"):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return AuthorizedScope(
                context=TenantContext(
                    tenant_id=self.tenant,
                    actor=UserActorContext(
                        actor_id=FirebaseUid(user),
                        allowed_scopes=frozenset({"notes:write"}),
                        authorization_version=member["authorization_version"],
                    ),
                ),
                scope=Scope(visibility=Visibility(visibility), display_policy=DisplayPolicy.NEVER),
                owner_user_id=owner,
            )

        return authorize

    def create(
        self, *, title: str = "synthetic note", key: str = KEY, authorize: Authorize | None = None
    ) -> MutationResult:
        def mutate(transaction: Transaction, current: AuthorizedScope) -> MutationResult:
            resource_id = ResourceId(str(uuid4()))
            path = self.collection + "/" + resource_id
            current.require_tenant_path(path)
            transaction.create(
                path,
                {
                    "title": title,
                    "version": 1,
                    "visibility": "shared",
                    "created_by": "demo-a-owner",
                },
            )
            return MutationResult(status_code=201, resource_id=resource_id, version=1)

        return self.idempotency.execute(
            operation="notes.create",
            key=key,
            body={"title": title},
            authorize=authorize or self.authorizer(),
            mutate=mutate,
        )

    def concurrently(
        self, first: Callable[[], MutationResult], second: Callable[[], MutationResult]
    ) -> list[MutationResult | ErrorCode]:
        barrier = Barrier(2)

        def invoke(work: Callable[[], MutationResult]) -> MutationResult | ErrorCode:
            barrier.wait(timeout=10)
            try:
                return work()
            except APIError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(invoke, work) for work in (first, second)]
            return [future.result(timeout=60) for future in futures]

    def records(self) -> list[dict[str, Any]]:
        return [
            snapshot.to_dict() or {}
            for snapshot in self.client.collection(
                f"tenants/{self.tenant}/idempotency_records"
            ).stream()
        ]

    def test_concurrent_create_same_body_converges_and_body_change_conflicts(self) -> None:
        results = self.concurrently(self.create, self.create)
        self.assertIsInstance(results[0], MutationResult)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(list(self.client.collection(self.collection).stream())), 1)
        self.assertEqual(len(self.records()), 1)
        with self.assertRaises(APIError) as conflict:
            self.create(title="replaced body")
        self.assertEqual(conflict.exception.code, ErrorCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(len(list(self.client.collection(self.collection).stream())), 1)

    def test_concurrent_different_body_commits_only_one(self) -> None:
        results = self.concurrently(self.create, lambda: self.create(title="different body"))
        self.assertEqual(sum(isinstance(result, MutationResult) for result in results), 1)
        self.assertIn(ErrorCode.IDEMPOTENCY_CONFLICT, results)
        self.assertEqual(len(list(self.client.collection(self.collection).stream())), 1)

    def test_commit_response_loss_replays_from_durable_record(self) -> None:
        commit = self.client._firestore_api.commit

        def lose_response(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic post-commit response loss")  # type: ignore[no-untyped-call]

        with patch.object(self.client._firestore_api, "commit", side_effect=lose_response):
            with self.assertRaises(APIError) as lost:
                self.create()
        self.assertEqual(lost.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
        recorded = MutationResult.model_validate(self.records()[0]["result"])
        # A fresh helper models a new API process; there is no in-memory result cache.
        self.idempotency = Idempotency(FirestoreTransactions(self.client), now=lambda: self.time)
        self.assertEqual(self.create(), recorded)
        self.assertEqual(len(list(self.client.collection(self.collection).stream())), 1)

    def test_authorization_revocation_version_scope_and_expiry_on_replay(self) -> None:
        created = self.create()
        member = self.client.document(f"tenants/{self.tenant}/memberships/demo-a-owner")
        for patch_data in ({"status": "revoked"}, {"status": "active", "authorization_version": 2}):
            member.update(patch_data)
            with self.assertRaises(APIError) as denied:
                self.create()
            self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)
        member.update({"authorization_version": 1})
        path = self.collection + "/" + str(created.resource_id)
        self.client.document(path).update({"visibility": "private"})
        with self.assertRaises(APIError) as denied:
            self.create()
        self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)
        self.time += timedelta(hours=24)
        again = self.create(title="new operation after TTL")
        self.assertNotEqual(again.resource_id, created.resource_id)
        record = self.records()[0]
        self.assertEqual(record["expires_at"] - record["created_at"], timedelta(hours=24))
        self.assertIsInstance(record["expires_at"], datetime)

    def test_t012_creator_and_admin_version_race_and_member_denial(self) -> None:
        resource_id = ResourceId(str(uuid4()))
        path = self.collection + "/" + resource_id
        self.client.document(path).set(
            {"title": "before", "version": 1, "visibility": "shared", "created_by": "demo-a-owner"}
        )

        def update(user: str, header: str | None = '"1"') -> MutationResult:
            def mutate(transaction: Transaction, current: AuthorizedScope) -> MutationResult:
                next_version = compare_and_write(
                    transaction,
                    authorized_scope=current,
                    path=path,
                    if_match=header,
                    replacement={
                        "title": user,
                        "visibility": "shared",
                        "created_by": "demo-a-owner",
                    },
                )
                return MutationResult(
                    status_code=200, resource_id=resource_id, version=next_version
                )

            return self.idempotency.execute(
                operation="notes.update:" + resource_id,
                key=KEY,
                body={"title": user},
                authorize=self.authorizer(user, path),
                mutate=mutate,
            )

        with self.assertRaises(APIError) as missing:
            update("demo-a-owner", None)
        self.assertEqual(missing.exception.code, ErrorCode.VERSION_REQUIRED)
        results = self.concurrently(lambda: update("demo-a-owner"), lambda: update("demo-a-admin"))
        self.assertEqual(sum(isinstance(result, MutationResult) for result in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        snapshot = self.client.document(path).get().to_dict()
        assert snapshot is not None
        self.assertEqual(snapshot["version"], 2)
        self.assertIn(snapshot["title"], ("demo-a-owner", "demo-a-admin"))
        self.assertEqual(len(self.records()), 1)
        with self.assertRaises(APIError) as denied:
            update("demo-a-member", '"2"')
        self.assertEqual(denied.exception.code, ErrorCode.OPERATION_FORBIDDEN)

    def test_deleted_created_resource_is_not_returned_from_receipt(self) -> None:
        created = self.create()
        self.client.document(self.collection + "/" + str(created.resource_id)).delete()
        with self.assertRaises(APIError) as deleted:
            self.create()
        self.assertEqual(deleted.exception.code, ErrorCode.RESOURCE_NOT_FOUND)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(list(self.client.collection(self.collection).stream())), 0)

    def test_failed_business_mutation_does_not_save_a_receipt_or_partial_writes(self) -> None:
        path = self.collection + "/" + str(uuid4())

        def fail(transaction: Transaction, current: AuthorizedScope) -> MutationResult:
            transaction.create(path, {"title": "must roll back"})
            raise RuntimeError("synthetic business failure")

        with self.assertRaises(RuntimeError):
            self.idempotency.execute(
                operation="notes.create", key=KEY, body={}, authorize=self.authorizer(), mutate=fail
            )
        self.assertFalse(self.client.document(path).get().exists)
        self.assertEqual(self.records(), [])

    def test_cursor_rechecks_current_database_authorization(self) -> None:
        codec = Cursors(b"synthetic-emulator-signing-key-001", now=lambda: self.time)

        def authorize() -> AuthorizedScope:
            return self.store.run(lambda tx: self.authorizer()(tx, None))

        token = codec.issue(
            position=str(uuid4()), query={"operation": "notes.list"}, authorize=authorize
        )
        member = self.client.document(f"tenants/{self.tenant}/memberships/demo-a-owner")
        member.update({"authorization_version": 2})
        with self.assertRaises(APIError) as changed:
            codec.read(token, query={"operation": "notes.list"}, authorize=authorize)
        self.assertEqual(changed.exception.code, ErrorCode.INVALID_REQUEST)
        member.update({"status": "revoked"})
        with self.assertRaises(APIError) as revoked:
            codec.read(token, query={"operation": "notes.list"}, authorize=authorize)
        self.assertEqual(revoked.exception.code, ErrorCode.OPERATION_FORBIDDEN)


if __name__ == "__main__":
    unittest.main()

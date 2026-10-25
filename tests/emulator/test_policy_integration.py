"""Real current-state policy reads in Firestore transactions on the fixed demo emulator."""

import os
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import uuid4

from brainoid_api.auth.authorization import authorized_scope, policy_call, transaction_policy
from brainoid_api.common.access import AuthorizedScope
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.idempotency import Idempotency, MutationResult
from brainoid_api.common.pagination import Cursors
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import FirebaseUid, ResourceId, Visibility
from brainoid_policy.models import Action, DeviceSubject, ResourceKind, ResourceRef, UserSubject
from brainoid_policy.scopes import ResourceGrant
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client, FieldFilter

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture


class PolicyIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.emulator = DemoEmulator(os.environ)
        self.emulator.reset()
        self.addCleanup(self.emulator.reset)
        self.emulator.seed()
        self.client = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.client.close)
        self.store = FirestoreTransactions(self.client)
        self.now = datetime(2026, 10, 26, tzinfo=UTC)
        documents = load_fixture()["documents"]
        self.tenant = ResourceId(documents[0]["fields"]["tenant_id"])
        self.owner = UserSubject(user_id=FirebaseUid("demo-a-owner"))
        self.member = UserSubject(user_id=FirebaseUid("demo-a-member"))
        self.admin = UserSubject(user_id=FirebaseUid("demo-a-admin"))
        self.refs = [
            ResourceRef(
                tenant_id=ResourceId(d["fields"]["tenant_id"]),
                kind=ResourceKind.NOTE,
                id=ResourceId(d["fields"]["id"]),
            )
            for d in documents
            if "/notes/" in d["path"]
        ]
        self.private, self.shared = self.refs[:2]
        device = next(
            d["fields"]
            for d in documents
            if "/devices/" in d["path"] and d["fields"]["status"] == "active"
        )
        self.device = DeviceSubject(device_id=ResourceId(device["id"]), connection_generation=1)
        self.device_path = f"tenants/{self.tenant}/devices/{device['id']}"

    def path(self, ref: ResourceRef) -> str:
        return f"tenants/{ref.tenant_id}/{ref.kind}/{ref.id}"

    def grant(
        self,
        subject: UserSubject | DeviceSubject,
        ref: ResourceRef,
        *,
        output: DeviceSubject | None = None,
    ) -> ResourceGrant:
        return self.store.run(
            lambda tx: policy_call(
                lambda: transaction_policy(tx, now=lambda: self.now).resource(
                    self.tenant, subject, ref, output=output
                )
            )
        )

    def denied(
        self, work: Callable[[], object], code: ErrorCode = ErrorCode.RESOURCE_NOT_FOUND
    ) -> None:
        with self.assertRaises(APIError) as error:
            work()
        self.assertEqual(error.exception.code, code)

    def test_t001_t002_t020_real_tenant_private_and_search_isolation(self) -> None:
        for subject in (self.owner, self.admin, self.member, self.device):
            for ref in self.refs:
                with self.subTest(subject=subject, ref=ref):
                    allowed = ref == self.shared or (subject == self.member and ref == self.private)
                    if allowed:
                        self.grant(subject, ref)
                    else:
                        self.denied(partial(self.grant, subject, ref))
        scope = self.store.run(
            lambda tx: transaction_policy(tx, now=lambda: self.now).search(
                self.tenant, self.device, ResourceKind.NOTE
            )
        )
        bodies = []
        # Apply visibility/display predicates before candidate retrieval. The current
        # canonical resource is authorized again before any body reaches its consumer.
        for f in scope.filters:
            self.assertEqual(f.visibility, Visibility.SHARED)
            query = (
                self.client.collection(f"tenants/{self.tenant}/notes")
                .where(filter=FieldFilter("visibility", "==", f.visibility))
                .where(filter=FieldFilter("display_policy", "in", sorted(f.display_policies)))
            )
            for snapshot in query.stream():
                ref = ResourceRef(
                    tenant_id=self.tenant, kind=ResourceKind.NOTE, id=ResourceId(snapshot.id)
                )
                self.grant(self.device, ref)
                bodies.append((snapshot.to_dict() or {})["body"])
        self.assertEqual(bodies, ["FIXTURE_A_SHARED_SYNTHETIC_NOTE"])

    def test_membership_and_resource_reads_are_current_on_each_transaction(self) -> None:
        saved = self.grant(self.member, self.private)
        member_path = f"tenants/{self.tenant}/memberships/{self.member.user_id}"
        self.client.document(member_path).update({"authorization_version": 2})
        self.denied(
            lambda: self.store.run(
                lambda tx: policy_call(
                    lambda: transaction_policy(tx, now=lambda: self.now).worker(saved)
                )
            )
        )
        current = self.grant(self.member, self.private)
        self.assertEqual(current.context.actor.authorization_version, 2)
        self.client.document(self.path(self.private)).update({"deleted": True, "deletion_epoch": 1})
        self.denied(partial(self.grant, self.member, self.private))
        self.client.document(member_path).update({"status": "revoked"})
        self.denied(partial(self.grant, self.member, self.shared))

    def test_actual_policy_revalidates_idempotency_and_cursor(self) -> None:
        def authorize(tx: Transaction, replay: MutationResult | None) -> AuthorizedScope:
            ref = (
                self.shared
                if replay is None
                else self.shared.model_copy(update={"id": replay.resource_id})
            )
            return policy_call(
                lambda: authorized_scope(
                    transaction_policy(tx, now=lambda: self.now).resource(
                        self.tenant, self.admin, ref, action=Action.WRITE
                    )
                )
            )

        def mutate(tx: Transaction, scope: AuthorizedScope) -> MutationResult:
            document = tx.get(self.path(self.shared))
            assert document is not None
            document.update({"version": 2, "body": "FIXTURE_UPDATED"})
            tx.set(self.path(self.shared), document)
            return MutationResult(status_code=200, resource_id=self.shared.id, version=2)

        idempotency = Idempotency(self.store, now=lambda: self.now)
        execute = partial(
            idempotency.execute,
            operation=f"notes.update:{self.shared.id}",
            key="policy-emulator-key-001",
            body={"body": "FIXTURE_UPDATED"},
            authorize=authorize,
            mutate=mutate,
        )
        result = execute()
        self.assertEqual(execute(), result)

        def current() -> AuthorizedScope:
            return self.store.run(lambda tx: authorize(tx, None))

        cursors = Cursors(b"c" * 32, now=lambda: self.now)
        cursor = cursors.issue(position=self.shared.id, query={"kind": "notes"}, authorize=current)
        self.assertEqual(
            cursors.read(cursor, query={"kind": "notes"}, authorize=current), self.shared.id
        )
        self.client.document(f"tenants/{self.tenant}").update({"policy_version": 2})
        self.denied(execute, ErrorCode.OPERATION_FORBIDDEN)
        self.denied(
            partial(cursors.read, cursor, query={"kind": "notes"}, authorize=current),
            ErrorCode.INVALID_REQUEST,
        )
        self.client.document(f"tenants/{self.tenant}/memberships/{self.admin.user_id}").update(
            {"role": "member", "authorization_version": 2}
        )
        self.denied(execute, ErrorCode.OPERATION_FORBIDDEN)
        self.assertEqual(self.client.document(self.path(self.shared)).get().to_dict()["version"], 2)  # type: ignore[index]

    def test_output_worker_checks_current_device_session_and_member(self) -> None:
        session_id = ResourceId(str(uuid4()))
        session_path = f"tenants/{self.tenant}/private_sessions/{session_id}"
        self.client.document(session_path).set(
            {
                "id": session_id,
                "tenant_id": self.tenant,
                "device_id": self.device.device_id,
                "user_id": self.member.user_id,
                "status": "active",
                "authorization_version": 1,
                "connection_generation": 1,
                "allowed_scopes": ["notes"],
                "expires_at": self.now + timedelta(minutes=5),
                "idle_expires_at": self.now + timedelta(seconds=60),
            }
        )
        personal = self.device.model_copy(update={"private_session_id": session_id})
        saved = self.grant(self.member, self.private, output=personal)

        def output() -> ResourceGrant:
            return self.store.run(
                lambda tx: policy_call(
                    lambda: transaction_policy(tx, now=lambda: self.now).worker(
                        saved, output=personal
                    )
                )
            )

        self.client.document(session_path).update(
            {"idle_expires_at": self.now + timedelta(seconds=90)}
        )
        output()
        self.client.document(session_path).update({"status": "revoked"})
        self.denied(output)
        self.client.document(session_path).update({"status": "active"})
        self.client.document(self.device_path).update({"authorization_version": 2})
        self.denied(output)
        saved = self.grant(self.member, self.private, output=personal)
        self.client.document(f"tenants/{self.tenant}/memberships/{self.member.user_id}").update(
            {"status": "suspended"}
        )
        self.denied(output)

    def test_opaque_uid_and_duplicate_membership_deny_in_real_transaction(self) -> None:
        uid = FirebaseUid("opaque/UID")
        document = (
            self.client.document(f"tenants/{self.tenant}/memberships/demo-a-member").get().to_dict()
        )
        assert document is not None
        document["user_id"] = uid
        self.client.document(f"tenants/{self.tenant}/memberships/safe-key").set(document)
        subject = UserSubject(user_id=uid)
        self.grant(subject, self.shared)
        self.client.document(f"tenants/{self.tenant}/memberships/duplicate-safe-key").set(document)
        self.denied(partial(self.grant, subject, self.shared))

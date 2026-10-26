"""MEM-01: real Firestore paths, atomic version checks and current source fences."""

import os
import unittest
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from brainoid_contracts import FirebaseUid, TenantContext
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_memory import CanonicalSources, EntityPatch, MemoryRepository, VersionConflict
from brainoid_persistence.firestore import FirestoreTransactions
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceSubject, UserSubject
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import (
    BEDROOM_LIGHT,
    FACT,
    LIGHT,
    NOW,
    OTHER,
    OTHER_PERSON,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    AuthenticatedCaller,
    GraphStore,
    authority_data,
    context,
    device_session,
    entity_values,
    fact_values,
)
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator


class _ReadHook:
    """Delegate every operation to the real SDK transaction, with a clock hook."""

    def __init__(self, tx: Transaction, after: Callable[[str], None]) -> None:
        self.tx, self.after = tx, after

    def get(self, path: str) -> Document | None:
        result = self.tx.get(path)
        self.after(path)
        return result

    def collection(self, path: str) -> list[tuple[str, Document]]:
        return self.tx.collection(path)

    def find(self, collection: str, field: str, value: str) -> list[Document]:
        return self.tx.find(collection, field, value)

    def memberships(self, user_id: str) -> list[tuple[str, Document]]:
        return self.tx.memberships(user_id)

    def create(self, path: str, document: Document) -> None:
        self.tx.create(path, document)

    def set(self, path: str, document: Document) -> None:
        self.tx.set(path, document)

    def delete(self, path: str) -> None:
        self.tx.delete(path)


class _DelayedReads:
    def __init__(self, store: TransactionStore, after: Callable[[str], None]) -> None:
        self.store, self.after = store, after

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        return self.store.run(lambda tx: work(_ReadHook(tx, self.after)))


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        self.db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.db.close)
        for path, document in authority_data().items():
            self.db.document(path).set(document)
        self.now = NOW
        self.caller = AuthenticatedCaller()
        self.store = FirestoreTransactions(self.db)
        self.repo = self.repository(self.store)
        self.context = context()
        self.repo.create_entity(self.context, LIGHT, entity_values())
        self.repo.create_entity(self.context, PERSON, entity_values(kind="person", label="Alex"))

    def repository(self, store: TransactionStore) -> MemoryRepository:
        return MemoryRepository(
            store,
            authorizer=self.caller,
            sources=CanonicalSources(),
            ledger=ControlLedger(MemoryObjects()),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )

    def session(self) -> tuple[DeviceSubject, TenantContext]:
        fixture = GraphStore()
        fixture.data = authority_data()
        subject, ctx = device_session(fixture)
        for path, document in fixture.data.items():
            if "/devices/" in path or "/private_session" in path:
                self.db.document(path).set(document)
        self.db.document(f"tenants/{TENANT}/memberships/safe-key").update({"role": "member"})
        return subject, ctx

    def test_member_pi_author_can_retract_from_device_and_phone(self) -> None:
        subject, ctx = self.session()
        for identity, from_phone in ((FACT, False), (BEDROOM_LIGHT, True)):
            self.caller.subject = subject
            created = self.repo.create_fact(ctx, identity, fact_values())
            self.assertEqual(
                (created.created_by, created.created_via_device_id), (USER, subject.device_id)
            )
            if from_phone:
                self.caller.subject = UserSubject(user_id=USER)
            result = self.repo.update_fact(
                self.context if from_phone else ctx,
                identity,
                status="retracted",
                expected_version=1,
                expected_deletion_epoch=0,
            )
            self.assertEqual((result.status, result.version), ("retracted", 2))
        self.caller.subject = subject
        self.repo.create_fact(ctx, OTHER_PERSON, fact_values())
        other = FirebaseUid("another-member")
        self.db.document(f"tenants/{TENANT}/memberships/other").set(
            {
                "tenant_id": TENANT,
                "user_id": other,
                "role": "member",
                "status": "active",
                "authorization_version": 1,
            }
        )
        self.caller.subject = UserSubject(user_id=other)
        with self.assertRaises(PolicyDenied):
            self.repo.update_fact(
                context(user=other),
                OTHER_PERSON,
                status="retracted",
                expected_version=1,
                expected_deletion_epoch=0,
            )

    def test_expiry_after_last_sdk_read_blocks_model_return_and_private_update(self) -> None:
        end = NOW + timedelta(seconds=60)
        self.repo.create_fact(self.context, FACT, fact_values(valid_to=end))
        source_path = f"tenants/{TENANT}/notes/{SOURCE}"
        reads = 0

        def after_source(path: str) -> None:
            nonlocal reads
            if path == source_path:
                reads += 1
                if reads == 3:
                    self.now = end

        delayed = self.repository(_DelayedReads(self.store, after_source))
        with self.assertRaises(PolicyDenied):
            delayed.get_fact(self.context, FACT, for_model=True)
        self.now = NOW
        self.repo.create_entity(
            self.context,
            BEDROOM_LIGHT,
            entity_values(visibility="private", display_policy="authenticated_session"),
        )
        self.caller.subject, ctx = self.session()
        target = f"tenants/{TENANT}/entities/{BEDROOM_LIGHT}"
        reads = 0

        def after_entity(path: str) -> None:
            nonlocal reads
            if path == target:
                reads += 1
                if reads == 2:
                    self.now = end

        delayed = self.repository(_DelayedReads(self.store, after_entity))
        with self.assertRaises(PolicyDenied):
            delayed.update_entity(
                ctx,
                BEDROOM_LIGHT,
                EntityPatch(label="At expiry"),
                expected_version=1,
                expected_deletion_epoch=0,
            )
        self.assertEqual((self.db.document(target).get().to_dict() or {})["version"], 1)

    def test_t009_equal_labels_and_local_ids_do_not_merge(self) -> None:
        self.repo.create_entity(self.context, BEDROOM_LIGHT, entity_values())
        self.repo.create_entity(context(OTHER), LIGHT, entity_values(label="OTHER_TENANT"))
        self.assertEqual(self.repo.get_entity(self.context, BEDROOM_LIGHT).label, "照明")
        self.assertEqual(self.repo.get_entity(self.context, LIGHT).label, "照明")
        self.assertEqual(self.repo.get_entity(context(OTHER), LIGHT).label, "OTHER_TENANT")
        self.db.document(f"tenants/{OTHER}/memberships/safe-key").delete()
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(context(OTHER), LIGHT)

    def test_competing_updates_commit_only_one_version(self) -> None:
        barrier = Barrier(2)

        def update(label: str) -> int | str:
            barrier.wait(timeout=10)
            try:
                return self.repo.update_entity(
                    self.context,
                    LIGHT,
                    EntityPatch(label=label),
                    expected_version=1,
                    expected_deletion_epoch=0,
                ).version
            except VersionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(update, ("Bedroom", "Living room")))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(self.repo.get_entity(self.context, LIGHT).version, 2)

    def test_fact_source_scope_version_and_suppression_are_current(self) -> None:
        source = self.db.document(f"tenants/{TENANT}/notes/{SOURCE}")
        source.update({"visibility": "private", "display_policy": "never"})
        with self.assertRaises(PolicyDenied):
            self.repo.create_fact(self.context, FACT, fact_values())
        self.assertFalse(self.db.document(f"tenants/{TENANT}/facts/{FACT}").get().exists)
        saved = self.repo.create_fact(
            self.context, FACT, fact_values(visibility="private", display_policy="never")
        )
        self.assertEqual(self.repo.get_fact(self.context, FACT, for_model=True), saved)
        source.update({"suppress_reuse": True})
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        self.assertEqual(self.repo.get_fact(self.context, FACT), saved)
        source.update({"version": 2})
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT)

    def test_current_membership_and_tombstone_block_reads_and_updates(self) -> None:
        self.repo.create_fact(self.context, FACT, fact_values())
        self.db.document(f"tenants/{TENANT}/entities/{LIGHT}").update(
            {"deleted": True, "deletion_epoch": 1}
        )
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(self.context, FACT, for_model=True)
        with self.assertRaises(PolicyDenied):
            self.repo.update_entity(
                self.context,
                LIGHT,
                EntityPatch(label="Wrong"),
                expected_version=1,
                expected_deletion_epoch=0,
            )
        self.db.document(f"tenants/{TENANT}/memberships/safe-key").update({"status": "revoked"})
        with self.assertRaises(PolicyDenied):
            self.repo.get_entity(self.context, PERSON)

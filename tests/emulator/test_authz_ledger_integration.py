"""Real Firestore transactions with the independent fault-injected control bucket."""

import os
import unittest
from collections.abc import Callable
from uuid import uuid4

from brainoid_api.auth.authorization import TransactionPolicyState
from brainoid_api.common.firestore import FirestoreTransactions
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_policy.control_ledger import Change, ControlLedger, LedgerError, MemberTarget
from brainoid_policy.models import Role
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator, load_fixture


class IndependentBucket:
    """A durable bucket separate from the actual emulator's restorable documents."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.before: Callable[[str], None] = lambda name: None
        self.after: Callable[[str], None] = lambda name: None

    def read(self, name: str) -> bytes | None:
        return self.data.get(name)

    def names(self, prefix: str) -> list[str]:
        return [name for name in self.data if name.startswith(prefix)]

    def create(self, name: str, data: bytes) -> bool:
        self.before(name)
        if name in self.data:
            return False
        self.data[name] = data
        self.after(name)
        return True


class ObservedFirestore:
    def __init__(self, store: FirestoreTransactions) -> None:
        self.store = store
        self.calls = 0
        self.after: Callable[[int], None] = lambda index: None
        self.in_transaction = False

    def run[T](self, work: Callable[[Transaction], T]) -> T:
        self.calls += 1
        call = self.calls

        def invoke(tx: Transaction) -> T:
            self.in_transaction = True
            try:
                return work(tx)
            finally:
                self.in_transaction = False

        result = self.store.run(invoke)
        self.after(call)
        return result


class AuthzLedgerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        emulator.seed()
        self.client = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(self.client.close)
        self.store = ObservedFirestore(FirestoreTransactions(self.client))
        self.objects = IndependentBucket()
        self.objects.before = lambda name: self.assertFalse(self.store.in_transaction)
        self.ledger = ControlLedger(self.objects)
        self.service = AuthzChanges(self.store, self.ledger)
        fixture = next(
            item
            for item in load_fixture()["documents"]
            if "/memberships/" in item["path"] and item["fields"]["user_id"] == "demo-a-member"
        )
        self.tenant = ResourceId(fixture["fields"]["tenant_id"])
        self.uid = FirebaseUid("demo-a-member")
        self.path = fixture["path"]
        self.backup = self.client.document(self.path).get().to_dict()

    def proposal(self, *, version: int = 2, grant: bool = False) -> Change:
        return Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=self.tenant,
            expected_version=version - 1,
            authorization_version=version,
            effect="grant" if grant else "deny",
            target=MemberTarget(
                user_id=self.uid, role=Role.MEMBER, status="active" if grant else "revoked"
            ),
        )

    def execute(self, change: Change) -> None:
        def authorize(tx: Transaction, proposed: Change) -> None:
            # Endpoint-specific role/re-auth rules are IAM-06. The trusted fixture
            # callback still performs an actual transactional tenant read here.
            tenant = tx.get(f"tenants/{proposed.tenant_id}")
            if tenant is None or tenant.get("status") != "active":
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)

        self.service.execute(change, authorize=authorize)

    def status(self) -> str:
        state = self.store.run(
            lambda tx: TransactionPolicyState(tx, ledger=self.ledger).membership(
                self.tenant, self.uid
            )
        )
        assert state is not None
        return state.status

    def test_t028_old_database_reapplies_latest_revocation_once(self) -> None:
        grant = self.proposal(grant=True)
        self.execute(grant)
        revoke = self.proposal(version=3)
        self.execute(revoke)
        assert self.backup is not None
        self.client.document(self.path).set(self.backup)
        self.assertEqual(self.status(), "suspended")
        self.assertEqual(self.service.reapply(grant).operation_id, revoke.operation_id)
        first = self.client.document(self.path).get()
        self.service.reapply(grant)
        second = self.client.document(self.path).get()
        self.assertEqual(first.update_time, second.update_time)
        self.assertEqual(self.status(), "revoked")
        with self.assertRaises(APIError):
            self.execute(grant)

    def test_real_ambiguous_db_commit_and_lost_ledger_commit_retry(self) -> None:
        change = self.proposal(grant=True)

        def ambiguous(index: int) -> None:
            if index == 2:
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)

        self.store.after = ambiguous
        with self.assertRaises(APIError):
            self.execute(change)
        self.store.after = lambda index: None
        self.assertEqual(self.status(), "suspended")

        def lost(name: str) -> None:
            if "/commits/" in name:
                raise LedgerError()

        self.objects.after = lost
        with self.assertRaises(APIError):
            self.execute(change)
        self.assertEqual(self.status(), "suspended")
        self.objects.after = lambda name: None
        self.execute(change)
        self.assertEqual(self.status(), "active")

    def test_pending_grant_restore_never_commits_and_opaque_uid_stays_single_document(self) -> None:
        self.uid = FirebaseUid("synthetic/opaque/uid")
        self.client.document(self.path).update({"user_id": self.uid})
        change = self.proposal(grant=True)
        self.ledger.prepare(change)
        for _ in range(2):
            self.assertEqual(self.service.reapply(change).state, "suspended")
        self.assertFalse(any("commits/" in name for name in self.objects.data))
        self.assertEqual(self.status(), "suspended")
        self.assertEqual(
            len(list(self.client.collection(f"tenants/{self.tenant}/memberships").stream())), 3
        )

    def test_new_deny_after_committed_grant_before_activation_fences_real_db(self) -> None:
        grant, revoke = self.proposal(grant=True), self.proposal(version=3)

        def competing(name: str) -> None:
            if "/commits/" in name:
                self.ledger.prepare(revoke)

        self.objects.after = competing
        with self.assertRaises(APIError):
            self.execute(grant)
        self.objects.after = lambda name: None
        self.assertEqual(self.status(), "suspended")
        self.execute(revoke)
        self.assertEqual(self.status(), "revoked")

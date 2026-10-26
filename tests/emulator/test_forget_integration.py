"""MEM-07 real transaction contention, immutable local ledger and response loss."""

from typing import Any
from unittest.mock import patch

from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.errors import APIError, ErrorCode
from brainoid_contracts import ResourceId
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, Operation
from brainoid_policy.control_ledger import LedgerError
from google.api_core.exceptions import ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture


class ForgetIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.objects = LocalAuthzObjects(self.directory)
        self.memories.deletion_ledger = DeletionLedger(self.objects)
        self.parent, self.child, self.message = self.source()
        self.db.document(self.child).update({"created_by": self.identity().user_id})
        self.memory = self.create_memory()
        self.forget_body = ForgetCreate(
            kind="forget_memory", target_id=self.memory.id, expected_version=1
        )

    def forget(self, key: str = "forget-emulator-key") -> Operation:
        return self.memories.forget(self.identity(), self.tenant, self.forget_body, key)

    def test_forget_concurrent_same_key_creates_one_receipt_job_and_intent(self) -> None:
        results = self.concurrent(self.forget, self.forget)
        self.assertIsInstance(results[0], Operation)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.objects.names("deletion/v1/operations/")), 1)
        self.assertTrue((self.db.document(self.child).get().to_dict() or {})["suppress_reuse"])
        self.assertEqual(self.memories.list(self.identity(), self.tenant).items, [])
        jobs = list(self.db.collection(f"tenants/{self.tenant}/jobs").stream())
        self.assertEqual(sum((j.to_dict() or {})["kind"] == "memory_purge" for j in jobs), 1)

    def test_forget_ledger_then_failed_db_commit_and_lost_response_roll_forward(self) -> None:
        append = self.memories.deletion_ledger.append  # type: ignore[union-attr]
        original_commit = self.db._firestore_api.commit

        def record_then_fail(intent: Any) -> None:
            append(intent)
            self.db._firestore_api.commit = lambda **kwargs: (_ for _ in ()).throw(
                ServiceUnavailable("synthetic")  # type: ignore[no-untyped-call]
            )

        with patch.object(self.memories.deletion_ledger, "append", side_effect=record_then_fail):
            try:
                with self.assertRaises(APIError) as caught:
                    self.forget()
                self.assertEqual(caught.exception.code, ErrorCode.TEMPORARILY_UNAVAILABLE)
            finally:
                self.db._firestore_api.commit = original_commit
        self.assertEqual(len(self.objects.names("deletion/v1/operations/")), 1)
        self.assertFalse((self.db.document(self.child).get().to_dict() or {})["suppress_reuse"])
        lost = False

        def commit(**kwargs: Any) -> Any:
            nonlocal lost
            result = original_commit(**kwargs)
            writes = kwargs.get("request", {}).get("writes", [])
            if not lost and any("/operations/" in str(w) for w in writes):
                lost = True
                raise ServiceUnavailable("synthetic response loss")  # type: ignore[no-untyped-call]
            return result

        with patch.object(self.db._firestore_api, "commit", side_effect=commit):
            with self.assertRaises(APIError):
                self.forget()
        self.assertTrue(lost)
        accepted = self.forget()
        self.assertEqual(accepted.status, "pending")
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/operations").stream())), 1
        )

    def test_forget_reservation_blocks_revision_until_ledger_recovery(self) -> None:
        with patch.object(self.objects, "create", side_effect=LedgerError()):
            with self.assertRaises(APIError):
                self.forget()
        with self.assertRaises(APIError) as caught:
            self.revision(self.memory.id)
        self.assertEqual(caught.exception.code, ErrorCode.VERSION_CONFLICT)
        accepted = self.forget()
        self.assertEqual(
            self.memories.operation(
                self.identity(), self.tenant, ResourceId(accepted.operation_id)
            ),
            accepted,
        )

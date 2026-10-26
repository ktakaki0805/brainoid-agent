"""Admission retries reread the deletion barrier at the actual acceptance boundary."""

from typing import Any
from unittest.mock import patch

from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.memories import Memories, MemoryCreate
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import ForgetCreate, Operation, operation_path
from google.api_core.exceptions import Aborted
from test_turn_run_integration import TurnRunIntegrationFixture


class PurgeBarrierIntegrationTests(TurnRunIntegrationFixture):
    def test_admission_transaction_retry_after_forget_gets_new_epoch_at_same_timestamp(
        self,
    ) -> None:
        cursors = Cursors(b"p" * 32, now=lambda: self.now)
        entities = Entities(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        subject = entities.create(
            self.identity(),
            self.tenant,
            EntityCreate.model_validate(
                {
                    "kind": "person",
                    "label": "Alex",
                    "visibility": "shared",
                    "display_policy": "shared_display",
                }
            ),
            "barrier-person-key",
        ).id
        memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        memories.deletion_ledger = DeletionLedger(LocalAuthzObjects(self.directory))
        memory = memories.create(
            self.identity(),
            self.tenant,
            MemoryCreate.model_validate(
                {
                    "subject_entity_id": subject,
                    "predicate": "prefers",
                    "value": "tea",
                    "visibility": "shared",
                    "display_policy": "shared_display",
                }
            ),
            "barrier-memory-key",
        )
        conversation = self.conversation()
        original = self.db._firestore_api.commit
        accepted: list[Operation | None] = []

        def interleave(**kwargs: Any) -> Any:
            request = kwargs["request"]
            if not accepted and any("/runs/" in str(write) for write in request.get("writes", [])):
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                # Mark recursion before entering another real transaction.
                accepted.append(None)
                accepted[0] = memories.forget(
                    self.identity(),
                    self.tenant,
                    ForgetCreate(kind="forget_memory", target_id=memory.id, expected_version=1),
                    "barrier-forget-key",
                )
                raise Aborted("synthetic admission retry after accepted forget")  # type: ignore[no-untyped-call]
            return original(**kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=interleave):
            run = self.accept(conversation).run_id
        self.assertEqual(len(accepted), 1)
        operation = accepted[0]
        assert operation is not None
        saved = (
            self.db.document(operation_path(self.tenant, operation.operation_id)).get().to_dict()
        )
        current = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None and current is not None
        self.assertEqual(saved["purge_admission_epoch"], 1)
        self.assertEqual(current["purge_epoch"], 1)
        self.assertEqual(current["created_at"], saved["operation"]["created_at"])
        self.execute(run)
        self.assertEqual(self.saved(run).status, "succeeded")

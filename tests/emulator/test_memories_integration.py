"""MEM-03 real SDK atomic supersession, source contention and index completion fences."""

from functools import partial
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.memories import Memories, MemoryCreate, MemoryRevision, MemoryView
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_jobs.models import job_id
from brainoid_memory.retrieval import SearchInput
from google.api_core.exceptions import Aborted, ServiceUnavailable
from test_device_pairing_integration import DevicePairingIntegrationFixture


class MemoryIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        cursors = Cursors(b"m" * 32, now=lambda: self.now)
        self.memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        entities = Entities(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.subject = entities.create(
            self.identity(),
            self.tenant,
            EntityCreate.model_validate(
                {
                    "kind": "person",
                    "label": "Synthetic person",
                    "visibility": "shared",
                    "display_policy": "shared_display",
                }
            ),
            "memory-emulator-entity",
        ).id
        self.memory_body = MemoryCreate.model_validate(
            {
                "subject_entity_id": self.subject,
                "predicate": "prefers",
                "value": "tea",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )

    def create_memory(self, key: str = "memory-emulator-create") -> MemoryView:
        return self.memories.create(self.identity(), self.tenant, self.memory_body, key)

    def revision(self, memory: ResourceId, key: str = "memory-emulator-revise") -> MemoryView:
        return self.memories.revise(
            self.identity(),
            self.tenant,
            memory,
            MemoryRevision(replacement=self.memory_body),
            key,
            '"1"',
        )

    def source(self) -> tuple[str, str, ResourceId]:
        conversation, message = ResourceId(str(uuid4())), ResourceId(str(uuid4()))
        parent = f"tenants/{self.tenant}/conversations/{conversation}"
        child = parent + "/messages/" + message
        self.db.document(parent).set(
            {
                "id": conversation,
                "tenant_id": self.tenant,
                "version": 1,
                "owner_user_id": self.identity().user_id,
                "created_by": self.identity().user_id,
                "visibility": "shared",
                "display_policy": "shared_display",
                "deletion_epoch": 0,
                "deleted": False,
                "suppress_reuse": False,
            }
        )
        self.db.document(child).set(
            {
                "id": message,
                "tenant_id": self.tenant,
                "conversation_id": conversation,
                "version": 1,
                "deletion_epoch": 0,
                "deleted": False,
                "suppress_reuse": False,
                "role": "user",
                "text": "Synthetic message",
                "run_id": str(uuid4()),
                "created_at": self.now,
            }
        )
        self.memory_body = MemoryCreate.model_validate(
            {
                **self.memory_body.model_dump(exclude_none=True),
                "source_refs": [{"kind": "message", "resource_id": message, "version": 1}],
            }
        )
        return parent, child, message

    def test_concurrent_idempotent_create_immediate_get_and_revision_compare_swap(self) -> None:
        result = self.concurrent(self.create_memory, self.create_memory)
        self.assertIsInstance(result[0], MemoryView)
        self.assertEqual(result[0], result[1])
        old = self.create_memory()
        self.assertEqual(self.memories.get(self.identity(), self.tenant, old.id), old)
        self.assertEqual(old.source_refs, ())
        revisions = self.concurrent(
            partial(self.revision, old.id, "memory-race-first"),
            partial(self.revision, old.id, "memory-race-second"),
        )
        self.assertEqual(sum(isinstance(r, MemoryView) for r in revisions), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, revisions)
        previous = self.memories.get(self.identity(), self.tenant, old.id)
        self.assertEqual((previous.status, previous.version), ("superseded", 2))
        self.assertEqual(len(self.memories.list(self.identity(), self.tenant).items), 1)
        self.assertEqual(len(list(self.db.collection_group("jobs").stream())), 2)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 3)

    def test_failure_before_commit_and_lost_commit_response_are_atomic(self) -> None:
        with patch.object(
            self.db._firestore_api,
            "commit",
            side_effect=ServiceUnavailable("synthetic"),  # type: ignore[no-untyped-call]
        ):
            self.fails(self.create_memory, ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(len(list(self.db.collection_group("jobs").stream())), 0)
        commit = self.db._firestore_api.commit

        def lose(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic lost response")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lose):
            self.fails(self.create_memory, ErrorCode.TEMPORARILY_UNAVAILABLE)
        old = self.create_memory()
        self.assertEqual(old.version, 1)
        with patch.object(
            self.db._firestore_api,
            "commit",
            side_effect=ServiceUnavailable("synthetic"),  # type: ignore[no-untyped-call]
        ):
            self.fails(partial(self.revision, old.id), ErrorCode.TEMPORARILY_UNAVAILABLE)
        self.assertEqual(
            self.memories.get(self.identity(), self.tenant, old.id).status, "confirmed"
        )
        self.assertEqual(len(list(self.db.collection_group("jobs").stream())), 1)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 2)

    def test_message_parent_and_child_current_boundaries_hide_get_list_and_replay(self) -> None:
        parent, child, _ = self.source()
        created = self.create_memory()
        self.db.document(parent).update({"visibility": "private", "display_policy": "never"})
        self.fails(self.create_memory, ErrorCode.RESOURCE_NOT_FOUND)
        self.fails(
            partial(self.memories.get, self.identity(), self.tenant, created.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.assertEqual(self.memories.list(self.identity(), self.tenant).items, [])
        self.memory_body = MemoryCreate.model_validate(
            {
                **self.memory_body.model_dump(exclude_none=True),
                "visibility": "private",
                "display_policy": "never",
            }
        )
        own = self.create_memory("memory-own-source")
        other = self.identity().model_copy(update={"user_id": FirebaseUid("demo-a-admin")})
        self.fails(
            partial(self.memories.get, other, self.tenant, own.id), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.fails(
            partial(
                self.memories.create, other, self.tenant, self.memory_body, "memory-other-source"
            ),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        self.db.document(child).update({"version": 2})
        self.fails(
            partial(self.create_memory, "memory-source-old-version"), ErrorCode.RESOURCE_NOT_FOUND
        )

    def test_source_changed_between_sdk_attempts_rechecks_permissions(self) -> None:
        parent, _, _ = self.source()
        commit = self.db._firestore_api.commit
        changed = False

        def abort_then_revoke(*args: Any, **kwargs: Any) -> Any:
            nonlocal changed
            request = kwargs.get("request", {})
            if not changed and request.get("transaction"):
                changed = True
                # Deterministically inject an aborted attempt. Release real SDK
                # locks before the other writer changes the canonical source;
                # blocking that writer inside an open callback would deadlock.
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                self.db.document(parent).update({"suppress_reuse": True})
                raise Aborted("synthetic transaction retry boundary")  # type: ignore[no-untyped-call]
            return commit(*args, **kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=abort_then_revoke):
            self.fails(self.create_memory, ErrorCode.RESOURCE_NOT_FOUND)
        self.assertTrue(changed)
        self.assertEqual(len(list(self.db.collection_group("jobs").stream())), 0)
        self.assertEqual(len(list(self.db.collection_group("idempotency_records").stream())), 1)

    def test_old_index_work_cannot_complete_after_atomic_revision(self) -> None:
        old = self.create_memory()
        jid = job_id(self.tenant, "memory_index", old.id)
        path = f"tenants/{self.tenant}/jobs/{jid}"
        self.assertIsNone(self.memories.jobs.prepare_dispatch(self.tenant, jid))
        self.db.document(path).update({"status": "pending"})  # Synthetic future activation.
        delivery = self.memories.jobs.prepare_dispatch(self.tenant, jid)
        assert delivery is not None
        lease = self.memories.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        self.revision(old.id)
        target = f"tenants/{self.tenant}/test_index/{old.id}"
        self.assertFalse(
            self.memories.jobs.complete(
                lease, None, lambda tx, job, result: tx.set(target, {"value": "stale"})
            )
        )
        self.assertFalse(self.db.document(target).get().exists)
        saved = self.db.document(path).get().to_dict()
        assert saved is not None
        self.assertEqual(saved["status"], "obsolete")

    def test_scoped_search_canonical_revision_and_private_shared_entity(self) -> None:
        first = self.create_memory()
        private_body = MemoryCreate.model_validate(
            {
                **self.memory_body.model_dump(exclude_none=True),
                "visibility": "private",
                "display_policy": "never",
                "value": "PRIVATE_EMULATOR_MEMORY",
            }
        )
        private = self.memories.create(
            self.identity(), self.tenant, private_body, "private-search-memory"
        )
        found = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="Synthetic person"),
            "search-emulator-query",
        )
        self.assertEqual({item.id for item in found.items}, {first.id, private.id})
        revised = self.revision(first.id)
        found = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="Synthetic person"),
            "search-emulator-query",
        )
        self.assertEqual({item.id for item in found.items}, {revised.id, private.id})
        self.db.document(f"tenants/{self.tenant}/facts/{private.id}").update(
            {"owner_user_id": "other-owner"}
        )
        found = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="Synthetic person"),
            "search-emulator-query",
        )
        self.assertEqual([item.id for item in found.items], [revised.id])
        self.assertNotIn("PRIVATE_EMULATOR_MEMORY", found.model_dump_json())

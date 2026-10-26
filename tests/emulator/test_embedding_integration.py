"""MEM-05 real Firestore Vector storage, scoped nearest and transactional fencing."""

from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.app import create_app
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.memories import Memories, MemoryCreate, MemoryRevision
from brainoid_api.routes.agents import reader_identity
from brainoid_api.settings import load_settings
from brainoid_contracts import ResourceId, Scope
from brainoid_jobs.models import job_id
from brainoid_memory.retrieval import SearchInput
from brainoid_memory.vector_store import VectorSearch, scope_key
from brainoid_persistence.vectors import FirestoreVectorIndex, index_spec
from brainoid_worker.dispatcher import Dispatcher
from brainoid_worker.embed_memory import KIND, EmbeddingDispatcher, EmbedMemory
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from fakes.embeddings import CONFIG, FakeEmbeddings
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1 import Client
from test_device_pairing_integration import DevicePairingIntegrationFixture


class EmbeddingIntegrationTests(DevicePairingIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        cursors = Cursors(b"v" * 32, now=lambda: self.now)
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
                    "label": "Synthetic person",
                    "visibility": "shared",
                    "display_policy": "shared_display",
                }
            ),
            "embedding-person-key",
        )
        self.subject = subject.id
        self.memories = Memories(
            self.store,
            self.tenants,
            cursors,
            ledger=self.ledger,
            credentials=self.repo,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.memory_body = MemoryCreate.model_validate(
            {
                "subject_entity_id": subject.id,
                "predicate": "prefers",
                "value": "VECTOR_SHARED",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        self.fact = self.memories.create(
            self.identity(), self.tenant, self.memory_body, "embedding-fact-key"
        )
        self.embeddings = FakeEmbeddings()
        self.index = FirestoreVectorIndex(self.db, CONFIG, manifest=index_spec(CONFIG))
        self.jobs = self.memories.jobs
        self.handler = EmbedMemory(self.jobs, self.embeddings, self.index)
        self.queue = LocalQueue()
        self.dispatcher = EmbeddingDispatcher(self.handler, self.store, self.queue)
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})
        self.memories.vector_search = VectorSearch(self.embeddings, self.index)

    def execute(self) -> None:
        self.handler.activate_page(self.store, self.tenant)
        interactive_queue = LocalQueue()
        self.assertEqual(
            Dispatcher(self.jobs, self.store, interactive_queue).recover(self.tenant).dispatched, 0
        )
        self.assertEqual(len(interactive_queue.tasks), 0)
        self.dispatcher.recover(self.tenant)
        self.queue.drain(self.worker)

    def vector_path(self, identity: ResourceId) -> str:
        return f"tenants/{self.tenant}/{CONFIG.collection}/{identity}"

    def status(self) -> Any:
        identity = self.handler.activate(
            self.tenant, job_id(self.tenant, "memory_index", self.fact.id)
        )
        assert identity is not None
        return self.jobs.get(self.tenant, identity)

    def test_real_vector_query_prefilters_tenant_scope_and_rechecks_current_canonical(self) -> None:
        private = self.memories.create(
            self.identity(),
            self.tenant,
            MemoryCreate.model_validate(
                {
                    **self.memory_body.model_dump(exclude_none=True),
                    "visibility": "private",
                    "display_policy": "never",
                    "value": "PRIVATE_VECTOR_SENTINEL",
                }
            ),
            "embedding-private-key",
        )
        self.execute()
        self.assertEqual(self.status().status, "succeeded")
        self.assertEqual(self.status().checkpoints[0].state, "completed")
        partition = scope_key(
            self.tenant,
            Scope.model_validate({"visibility": "shared", "display_policy": "shared_display"}),
            None,
        )
        hits = self.index.nearest(self.tenant, partition, (1.0, 1.0, 1.0), limit=40)
        self.assertEqual([hit.id for hit in hits], [self.fact.id])
        other_tenant = ResourceId(str(uuid4()))
        self.assertEqual(self.index.nearest(other_tenant, partition, (1.0, 1.0, 1.0), limit=40), ())
        results = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="semantic-only"),
            "vector-emulator-search",
        )
        self.assertEqual({f.id for f in results.items}, {self.fact.id, private.id})
        self.db.document(f"tenants/{self.tenant}/facts/{private.id}").update(
            {"owner_user_id": "unrelated-user"}
        )
        results = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="semantic-only"),
            "vector-emulator-search",
        )
        self.assertEqual([f.id for f in results.items], [self.fact.id])
        self.assertNotIn("PRIVATE_VECTOR_SENTINEL", results.model_dump_json())
        self.db.document(f"tenants/{self.tenant}/facts/{self.fact.id}").update(
            {"suppress_reuse": True}
        )
        results = self.memories.search(
            self.identity(),
            self.tenant,
            SearchInput(query="semantic-only"),
            "vector-emulator-search",
        )
        self.assertEqual(results.items, [])

    def test_api_composition_factory_reaches_http_with_real_vector_client(self) -> None:
        self.execute()
        clients: list[Client] = []

        def factory(client: Client) -> VectorSearch:
            clients.append(client)
            return VectorSearch(
                self.embeddings, FirestoreVectorIndex(client, CONFIG, manifest=index_spec(CONFIG))
            )

        settings = load_settings().model_copy(update={"authz_ledger_directory": self.directory})
        app = create_app(settings_loader=lambda: settings, vector_search_factory=factory)
        app.dependency_overrides[reader_identity] = self.identity
        with TestClient(app) as http:
            response = http.post(
                f"/v1/tenants/{self.tenant}/memories:search",
                json={"query": "semantic-only"},
                headers={"Idempotency-Key": "vector-factory-http"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([item["id"] for item in response.json()["items"]], [self.fact.id])
        self.assertEqual(len(clients), 1)

    def test_revision_during_external_generation_is_obsolete_without_vector(self) -> None:
        identity = self.handler.activate(
            self.tenant, job_id(self.tenant, "memory_index", self.fact.id)
        )
        assert identity is not None

        def revise() -> None:
            self.memories.revise(
                self.identity(),
                self.tenant,
                self.fact.id,
                MemoryRevision(
                    replacement=self.memory_body.model_copy(update={"value": "replacement"})
                ),
                "vector-racing-revision",
                '"1"',
            )

        self.embeddings.hook = revise
        self.execute()
        self.assertEqual(self.jobs.get(self.tenant, identity).status, "obsolete")  # type: ignore[union-attr]
        self.assertFalse(self.db.document(self.vector_path(self.fact.id)).get().exists)
        self.assertEqual(len(self.embeddings.calls), 1)

    def test_sdk_retry_after_embedding_rechecks_entity_before_atomic_vector_checkpoint(
        self,
    ) -> None:
        commit = self.db._firestore_api.commit
        changed = False

        def abort(*args: Any, **kwargs: Any) -> Any:
            nonlocal changed
            request = kwargs.get("request", {})
            if not changed and self.embeddings.calls and request.get("transaction"):
                changed = True
                self.db._firestore_api.rollback(
                    request={
                        "database": self.db._database_string,
                        "transaction": request["transaction"],
                    }
                )
                self.db.document(f"tenants/{self.tenant}/entities/{self.subject}").update(
                    {"suppress_reuse": True}
                )
                raise Aborted("synthetic vector commit retry")  # type: ignore[no-untyped-call]
            return commit(*args, **kwargs)

        with patch.object(self.db._firestore_api, "commit", side_effect=abort):
            self.execute()
        self.assertTrue(changed)
        self.assertEqual(self.status().status, "obsolete")
        self.assertFalse(self.db.document(self.vector_path(self.fact.id)).get().exists)
        self.assertEqual(len(self.embeddings.calls), 1)
        self.assertEqual(self.status().checkpoints[0].state, "started")

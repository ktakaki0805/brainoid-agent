"""MEM-05 / T-010 T-011: generation fences and real SDK request contracts."""

import copy
import unittest
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import Delivery, Jobs
from brainoid_jobs.models import SourceObsolete, job_id
from brainoid_memory.dto import MemoryCreate
from brainoid_memory.mutations import MemoryMutations
from brainoid_memory.retrieval import Candidate, SearchInput, retrieve
from brainoid_memory.vector_store import (
    EmbeddingConfig,
    VectorConfigurationError,
    VectorSearch,
    VectorUnavailable,
)
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_persistence.vectors import FirestoreVectorIndex, index_spec
from brainoid_policy.control_ledger import ControlLedger
from brainoid_worker.embed_memory import KIND, EmbedMemory
from brainoid_worker.entry import Execution, Worker
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from fakes.authz_ledger import MemoryObjects
from fakes.embeddings import CONFIG, FakeEmbeddings
from fakes.memory_repository import (
    LIGHT,
    NOW,
    PERSON,
    SOURCE,
    TENANT,
    USER,
    GraphStore,
    authority_data,
    context,
    entity_values,
    repository,
)
from google.api_core.exceptions import FailedPrecondition
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client
from google.cloud.firestore_v1.vector_query import VectorQuery


class StoredVectors(FirestoreVectorIndex):
    def __init__(self, store: GraphStore, config: EmbeddingConfig = CONFIG) -> None:
        super().__init__(MagicMock(), config, manifest=index_spec(config))
        self.store = store
        self.partitions: list[str] = []

    def nearest(
        self, tenant: ResourceId, partition: str, vector: tuple[float, ...], *, limit: int
    ) -> tuple[Candidate, ...]:
        self.config.vector(vector)
        self.partitions.append(partition)
        return tuple(
            Candidate.model_validate({k: d[k] for k in ("id", "version", "deletion_epoch")})
            for p, d in self.store.data.items()
            if p.rsplit("/", 1)[0] == f"tenants/{tenant}/{self.config.collection}"
            and d["scope_key"] == partition
            and d["status"] == "ready"
        )[:limit]


class EmbeddingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW
        self.store = GraphStore()
        self.store.data = authority_data()
        self.repo = repository(self.store)
        self.repo.create_entity(context(), PERSON, entity_values(kind="person", label="Alex"))
        self.repo.create_entity(context(), LIGHT, entity_values(kind="topic", label="Lamp"))
        self.jobs = Jobs(
            self.store,
            ControlLedger(MemoryObjects()),
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.embeddings = FakeEmbeddings()
        self.index = StoredVectors(self.store)
        self.handler = EmbedMemory(self.jobs, self.embeddings, self.index)
        self.body = MemoryCreate.model_validate(
            {
                "subject_entity_id": PERSON,
                "predicate": "prefers",
                "value": "tea",
                "source_refs": [{"kind": "note", "resource_id": SOURCE, "version": 1}],
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        self.fact = MemoryMutations(self.jobs.store, self.jobs, now=lambda: self.now).mutate(
            lambda tx: self.repo.facts(tx, context()), self.body, "embedding-create-fixture"
        )
        self.outbox = job_id(TENANT, "memory_index", self.fact.id)
        identity = self.handler.activate(TENANT, self.outbox)
        assert identity is not None
        self.identity = identity
        self.worker = Worker(self.jobs, LocalCaller(), {KIND: self.handler})

    @property
    def vector_path(self) -> str:
        return f"tenants/{TENANT}/{self.index.config.collection}/{self.fact.id}"

    def deliver(self) -> None:
        assert self.identity is not None
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity)
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def state(self) -> Any:
        assert self.identity is not None
        return self.jobs.get(TENANT, self.identity)


class EmbeddingFencingTests(EmbeddingFixture):
    def test_durable_vector_checkpoint_and_success_without_body(self) -> None:
        # Old non-embedding job shapes remain readable by pre-MEM-05 binaries.
        self.assertNotIn("attributes", self.store.data[f"tenants/{TENANT}/jobs/{self.outbox}"])
        self.deliver()
        job = self.state()
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.checkpoints[0].state, "completed")
        self.assertEqual(len(self.embeddings.calls), 1)
        self.assertIn(self.vector_path, self.store.data)
        self.assertNotIn("tea", job.model_dump_json())
        self.assertNotIn(str(USER), self.embeddings.calls[0][1])
        self.assertEqual(self.handler.activate(TENANT, self.outbox), self.identity)
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))

    def test_changes_during_generation_drop_vector_and_finish_obsolete(self) -> None:
        baseline = copy.deepcopy(self.store.data)
        source = f"tenants/{TENANT}/facts/{self.fact.id}"
        for path, changes in (
            (source, {"version": 2, "status": "superseded"}),
            (source, {"deletion_epoch": 1, "deleted": True}),
            (source, {"suppress_reuse": True}),
            (f"tenants/{TENANT}/entities/{PERSON}", {"deleted": True}),
            (source, {"valid_to": NOW.isoformat()}),
            (f"tenants/{TENANT}/notes/{SOURCE}", {"suppress_reuse": True}),
            (f"tenants/{TENANT}/notes/{SOURCE}", {"version": 2}),
            (f"tenants/{TENANT}/memberships/safe-key", {"authorization_version": 2}),
        ):
            with self.subTest(changes=changes):
                self.store.data = copy.deepcopy(baseline)

                def mutate(path: str = path, changes: dict[str, Any] = changes) -> None:
                    self.store.data[path].update(changes)

                self.embeddings.hook = mutate
                self.deliver()
                self.assertEqual(self.state().status, "obsolete")
                self.assertNotIn(self.vector_path, self.store.data)

    def test_source_change_and_revocation_before_call_send_no_text(self) -> None:
        self.store.data[f"tenants/{TENANT}/entities/{PERSON}"]["suppress_reuse"] = True
        self.deliver()
        self.assertEqual(self.state().status, "obsolete")
        self.assertEqual(self.embeddings.calls, [])

    def test_expired_lease_never_commits_and_unknown_never_reexecutes(self) -> None:
        self.embeddings.hook = lambda: setattr(self, "now", self.now + timedelta(minutes=2))
        self.deliver()
        self.assertNotIn(self.vector_path, self.store.data)
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))
        self.assertEqual(self.state().reason, "outcome_unknown")
        self.assertEqual(len(self.embeddings.calls), 1)

    def test_provider_failure_is_unknown_with_no_automatic_retry(self) -> None:
        def fail() -> None:
            raise RuntimeError("synthetic response lost")

        self.embeddings.hook = fail
        self.deliver()
        self.assertEqual(self.state().reason, "outcome_unknown")
        self.assertNotIn(self.vector_path, self.store.data)
        self.assertIsNone(self.jobs.prepare_dispatch(TENANT, self.identity))
        self.assertEqual(len(self.embeddings.calls), 1)

    def test_completed_effect_resumes_without_rebilling_and_checks_missing_vector(self) -> None:
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: True)
        assert lease is not None
        result = self.handler.run(Execution(self.jobs, lease))
        self.assertEqual(self.state().status, "running")
        self.assertIn(self.vector_path, self.store.data)
        self.now += timedelta(minutes=2)
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: True)
        assert lease is not None
        replay = self.handler.run(Execution(self.jobs, lease))
        self.assertEqual(result, replay)
        self.assertEqual(len(self.embeddings.calls), 1)
        del self.store.data[self.vector_path]
        with self.assertRaises(SourceObsolete):
            self.jobs.complete(lease, replay, self.handler.commit)

    def test_completed_checkpoint_requires_vector_type_dimensions_scope_and_resumes_successfully(
        self,
    ) -> None:
        delivery = self.jobs.prepare_dispatch(TENANT, self.identity)
        assert delivery is not None
        lease = self.jobs.acquire(delivery, known_handler=lambda kind: True)
        assert lease is not None
        result = self.handler.run(Execution(self.jobs, lease))
        original = copy.deepcopy(self.store.data[self.vector_path])
        for changes in (
            {"embedding": None},
            {"embedding": [1.0, 2.0, 3.0]},
            {"scope_key": "unrelated-scope"},
        ):
            with self.subTest(changes=changes):
                self.store.data[self.vector_path] = {**original, **changes}
                with self.assertRaises(SourceObsolete):
                    self.jobs.complete(lease, result, self.handler.commit)
        self.store.data[self.vector_path] = original
        self.assertTrue(self.jobs.complete(lease, result, self.handler.commit))
        self.assertEqual(self.state().status, "succeeded")
        self.assertEqual(len(self.embeddings.calls), 1)

    def test_ambiguous_vector_commit_response_recovers_checkpoint_without_rebilling(self) -> None:
        def after_commit() -> None:
            if self.vector_path in self.store.data:
                self.store.after_commit = lambda: None
                raise PersistenceUnavailable()

        def arm() -> None:
            self.store.after_commit = after_commit

        self.embeddings.hook = arm
        with self.assertRaises(PersistenceUnavailable):
            self.deliver()
        self.assertEqual(self.state().status, "pending")
        self.assertEqual(self.state().checkpoints[0].state, "completed")
        self.assertIn(self.vector_path, self.store.data)
        self.now += timedelta(seconds=6)
        self.deliver()
        self.assertEqual(self.state().status, "succeeded")
        self.assertEqual(len(self.embeddings.calls), 1)

    def test_forget_before_dispatch_and_duplicate_delivery_never_recreates_vector(self) -> None:
        self.store.data[f"tenants/{TENANT}/facts/{self.fact.id}"].update(
            deleted=True, deletion_epoch=1
        )
        self.deliver()
        job = self.state()
        self.worker.handle(
            LOCAL_AUTHORIZATION,
            Delivery(
                tenant_id=TENANT, job_id=job.id, dispatch_generation=job.dispatch_generation
            ).model_dump(mode="json"),
        )
        self.assertEqual(self.state().status, "obsolete")
        self.assertEqual(self.embeddings.calls, [])
        self.assertNotIn(self.vector_path, self.store.data)
        self.assertIsNone(self.handler.activate(TENANT, self.outbox))

    def test_commit_retry_revalidates_dependencies_without_second_provider_call(self) -> None:
        def retry() -> None:
            self.store.retry = lambda: self.store.data[
                f"tenants/{TENANT}/entities/{PERSON}"
            ].update(suppress_reuse=True)

        self.embeddings.hook = retry
        self.deliver()
        self.assertEqual(len(self.embeddings.calls), 1)
        self.assertNotIn(self.vector_path, self.store.data)
        self.assertEqual(self.state().status, "obsolete")

    def test_new_generation_preserves_original_grant_and_old_index(self) -> None:
        self.deliver()
        config = EmbeddingConfig(model="synthetic-embedding", revision="fixture-v2", dimensions=2)
        new = EmbedMemory(self.jobs, FakeEmbeddings(config), StoredVectors(self.store, config))
        identity = new.activate(TENANT, self.outbox)
        self.assertNotEqual(identity, self.identity)
        assert identity is not None
        target = self.jobs.get(TENANT, identity)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.grant, self.state().grant)
        delivery = self.jobs.prepare_dispatch(TENANT, identity)
        assert delivery is not None
        Worker(self.jobs, LocalCaller(), {KIND: new}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        self.assertEqual(self.jobs.get(TENANT, identity).status, "succeeded")  # type: ignore[union-attr]
        self.assertIn(self.vector_path, self.store.data)
        self.assertIn(f"tenants/{TENANT}/{config.collection}/{self.fact.id}", self.store.data)

    def test_nearest_phase_then_current_canonical_and_scope_intersection(self) -> None:
        self.deliver()
        search = VectorSearch(self.embeddings, self.index)
        request = SearchInput(query="unrelated semantic query")

        def opened(tx: Transaction) -> Any:
            return self.repo.facts(tx, context())

        prepared = search.prepare(self.jobs.store, opened, request)

        def read(tx: Transaction) -> Any:
            access = opened(tx)
            return retrieve(access, access.entities(), request, provider=prepared)

        self.assertEqual([r.id for r in self.jobs.store.run(read).records], [self.fact.id])
        self.store.data[f"tenants/{TENANT}/facts/{self.fact.id}"]["suppress_reuse"] = True
        self.assertEqual(self.jobs.store.run(read).records, ())


class VectorSDKTests(unittest.TestCase):
    def test_configuration_dimensions_and_manifest_fail_closed(self) -> None:
        for dimension in (0, 2049):
            with self.assertRaises(ValueError):
                EmbeddingConfig(model="synthetic", revision="v1", dimensions=dimension)
        for vector in ((1.0, 2.0), (1.0, float("nan"), 2.0), (True, 1.0, 2.0)):
            with self.assertRaises(VectorConfigurationError):
                CONFIG.vector(vector)
        wrong = index_spec(CONFIG)
        wrong["indexes"][0]["fields"][-1]["vectorConfig"]["dimension"] = 4
        with self.assertRaises(VectorConfigurationError):
            FirestoreVectorIndex(MagicMock(), CONFIG, manifest=wrong)

    def test_official_sdk_builds_partition_filters_before_find_nearest_and_surfaces_missing_index(
        self,
    ) -> None:
        client = Client(project="demo-vector-test", credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(client.close)
        index = FirestoreVectorIndex(client, CONFIG, manifest=index_spec(CONFIG))
        captured: list[Any] = []

        def stream(query: VectorQuery, **kwargs: Any) -> list[Any]:
            captured.append(query._to_protobuf())
            self.assertEqual(kwargs, {"retry": None, "timeout": 10})
            return []

        with patch.object(VectorQuery, "stream", stream):
            self.assertEqual(index.nearest(TENANT, "partition", (1.0, 2.0, 3.0), limit=10), ())
        query = captured[0]
        self.assertEqual(query.from_[0].collection_id, CONFIG.collection)
        self.assertFalse(query.from_[0].all_descendants)
        self.assertEqual(
            [
                (f.field_filter.field.field_path, f.field_filter.value.string_value)
                for f in query.where.composite_filter.filters
            ],
            [("scope_key", "partition"), ("status", "ready")],
        )
        self.assertEqual(query.find_nearest.vector_field.field_path, "embedding")
        with patch.object(
            VectorQuery,
            "stream",
            side_effect=FailedPrecondition("index missing"),  # type: ignore[no-untyped-call]
        ):
            with self.assertRaises(VectorUnavailable):
                index.nearest(TENANT, "partition", (1.0, 2.0, 3.0), limit=10)


class GoogleEmbeddingSDKTests(unittest.TestCase):
    def test_single_sdk_request_explicit_task_dimensions_region_and_no_retry(self) -> None:
        import json

        import httpx
        from brainoid_model_google.embeddings import GoogleEmbeddings, GoogleEmbeddingSettings
        from google.oauth2.credentials import Credentials

        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200, json={"predictions": [{"embeddings": {"values": [1.0, 2.0, 3.0]}}]}
            )

        provider = GoogleEmbeddings(
            CONFIG,
            GoogleEmbeddingSettings(
                project="demo-vector-test", location="us-central1", timeout_seconds=5.0
            ),
            credentials=Credentials("synthetic-token"),  # type: ignore[no-untyped-call]
            transport_factory=lambda: httpx.MockTransport(respond),
        )
        self.assertEqual(provider.embed("synthetic text", task="document"), (1.0, 2.0, 3.0))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "us-central1-aiplatform.googleapis.com")
        body = json.loads(requests[0].content)
        self.assertEqual(body["instances"][0]["task_type"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(body["parameters"]["outputDimensionality"], 3)
        self.assertIs(body["parameters"]["autoTruncate"], False)
        requests.clear()

        def unavailable(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503, json={"error": {"message": "synthetic"}})

        provider.transport_factory = lambda: httpx.MockTransport(unavailable)
        with self.assertRaises(VectorUnavailable):
            provider.embed("synthetic", task="query")
        self.assertEqual(len(requests), 1)

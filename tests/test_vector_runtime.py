"""MEM-05 vector retrieval reaches HTTP and both Core/typed-tool memory paths."""

import json
from typing import Any

from brainoid_agent_core.types import ToolCall
from brainoid_contracts import ResourceId
from brainoid_memory.vector_store import VectorSearch, VectorUnavailable
from brainoid_runs.state import KIND as RUN_KIND
from brainoid_worker.dispatcher import Dispatcher
from brainoid_worker.embed_memory import KIND, EmbeddingDispatcher, EmbedMemory
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from fakes.embeddings import FakeEmbeddings
from fakes.memory_repository import TENANT
from test_embedding_fencing import StoredVectors
from test_memories import MemoryFixture
from test_scoped_retrieval import MemoryWorkerFixture


class VectorHTTPTests(MemoryFixture):
    def test_http_uses_semantic_hit_and_scope_current_canonical_filters(self) -> None:
        fact = self.create_memory().json()
        private = self.create_memory(
            key="vector-private-key",
            value="PRIVATE_SENTINEL",
            visibility="private",
            display_policy="never",
        ).json()
        embeddings = FakeEmbeddings()
        index = StoredVectors(self.store)
        handler = EmbedMemory(self.memories.jobs, embeddings, index)
        queue = LocalQueue()
        # New and pre-existing waiting jobs are discovered by the same bounded scan.

        # Fixture's store does not expose dispatcher paging; use bounded test pages.
        class Pages:
            def page(inner, collection: str, *, after: str | None = None, limit: int = 100) -> Any:
                return [
                    (p, d)
                    for p, d in sorted(self.store.data.items())
                    if p.rsplit("/", 1)[0] == collection
                    and (after is None or p.rsplit("/", 1)[-1] > after)
                ][:limit]

        dispatcher = EmbeddingDispatcher(handler, Pages(), queue)
        self.assertEqual(dispatcher.recover(TENANT).dispatched, 2)
        queue.drain(Worker(self.memories.jobs, LocalCaller(), {KIND: handler}))
        self.memories.vector_search = VectorSearch(embeddings, index)
        response = self.client.post(
            self.url + ":search",
            json={"query": "semantic-only"},
            headers={"Idempotency-Key": "semantic-search-key"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({r["id"] for r in response.json()["items"]}, {fact["id"], private["id"]})
        self.store.data[f"tenants/{TENANT}/facts/{private['id']}"]["owner_user_id"] = "another-user"
        response = self.client.post(
            self.url + ":search",
            json={"query": "semantic-only"},
            headers={"Idempotency-Key": "semantic-search-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("PRIVATE_SENTINEL", response.text)

        def fail() -> None:
            raise VectorUnavailable()

        embeddings.hook = fail
        response = self.client.post(
            self.url + ":search",
            json={"query": "tea"},
            headers={"Idempotency-Key": "semantic-search-key"},
        )
        self.assertEqual(response.status_code, 503)


class VectorWorkerTests(MemoryWorkerFixture):
    def test_core_and_typed_search_use_vectors_without_private_scope_widening(self) -> None:
        shared = self.memory()
        self.memory(value="PRIVATE_SENTINEL", visibility="private", display_policy="never")
        embeddings = FakeEmbeddings()
        index = StoredVectors(self.store)  # type: ignore[arg-type]
        handler = EmbedMemory(self.jobs, embeddings, index)
        queue = LocalQueue()
        dispatcher = EmbeddingDispatcher(handler, self.store, queue)  # type: ignore[arg-type]
        # The ordinary dispatcher must not steal another queue's newly activated jobs.
        originals = [d["id"] for d in self.store.data.values() if d.get("kind") == "memory_index"]
        for identity in originals:
            handler.activate(self.tenant, ResourceId(identity))
        interactive_queue = LocalQueue()
        interactive = Dispatcher(self.jobs, self.store, interactive_queue)  # type: ignore[arg-type]
        self.assertEqual(interactive.recover(self.tenant).dispatched, 0)
        self.assertEqual(len(interactive_queue.tasks), 0)
        self.assertEqual(dispatcher.recover(self.tenant).dispatched, 2)
        self.assertEqual(interactive.recover(self.tenant).dispatched, 0)
        queue.drain(Worker(self.jobs, LocalCaller(), {KIND: handler}))
        self.configure_calls(
            (
                ToolCall(
                    call_key="search",
                    name="memory.search",
                    arguments_json=json.dumps({"query": "semantic-only"}),
                ),
            )
        )
        self.handler.memory.vector_search = VectorSearch(embeddings, index)
        self.worker = Worker(self.jobs, LocalCaller(), {RUN_KIND: self.handler})
        index.partitions.clear()
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="semantic-only").json()["run_id"])
        self.assertEqual(dispatcher.recover(self.tenant).dispatched, 0)
        self.assertEqual(interactive.recover(self.tenant).dispatched, 1)
        interactive_queue.drain(self.worker)
        self.assertEqual(self.get_run(run).json()["status"], "completed", self.get_run(run).text)
        self.assertEqual(len(self.model.requests), 2)
        self.assertIn(shared.id, self.model.requests[0].model_dump_json())
        self.assertNotIn("PRIVATE_SENTINEL", self.model.requests[-1].model_dump_json())
        self.assertEqual(len(set(index.partitions)), 1)
        self.assertGreaterEqual(len([task for task, _ in embeddings.calls if task == "query"]), 2)

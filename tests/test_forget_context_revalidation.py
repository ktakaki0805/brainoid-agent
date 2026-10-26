"""MEM07-R1-01: self-forget never erases unrelated context invalidation."""

import copy
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from unittest.mock import patch

from brainoid_agent_core.types import ToolCall
from brainoid_contracts import ResourceId
from brainoid_jobs.models import Job, job_id
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger
from brainoid_memory.dto import MemoryCreate, MemoryRevision
from brainoid_memory.forget import ForgetCreate, ForgetMemories
from brainoid_policy.scopes import PolicyDenied
from fakes.authz_ledger import MemoryObjects
from test_memory_revalidation import MatchedClock
from test_scoped_retrieval import MemoryWorkerFixture


class ForgetContextTests(MemoryWorkerFixture):
    def configure_forget(self, target: ResourceId, *, search: bool = False) -> MemoryObjects:
        calls: tuple[ToolCall, ...] = (
            ToolCall(
                call_key="forget",
                name="memory.forget",
                arguments_json=json.dumps({"memory_id": target, "expected_version": 1}),
            ),
        )
        if search:
            calls = (
                ToolCall(
                    call_key="search", name="memory.search", arguments_json='{"query":"Alex"}'
                ),
                *calls,
            )
        self.configure_calls(calls)
        self.handler.clock = MatchedClock(self.clock.now)
        objects = MemoryObjects()
        self.handler.memory.deletion_ledger = DeletionLedger(objects)
        self.memories.deletion_ledger = DeletionLedger(MemoryObjects())
        return objects

    def external_change(
        self, change: Literal["forget", "revise", "expire"], *, search: bool = False
    ) -> None:
        target = self.memory(value="TARGET_B")
        other = self.memory(value="OTHER_A", valid_to=self.clock.now() + timedelta(seconds=1))
        objects = self.configure_forget(target.id, search=search)
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {target.id}").json()["run_id"]
        )
        path = f"tenants/{self.tenant}/runs/{run}"
        before: dict[str, Any] = {}

        def invalidate(name: str) -> None:
            objects.before = lambda name: None
            before.update(copy.deepcopy(self.store.data[path]))
            self.assertEqual(
                {r["memory_id"] for r in before["memory_records"]}, {target.id, other.id}
            )
            if change == "forget":
                self.memories.forget(
                    self.identity(),
                    self.tenant,
                    ForgetCreate(kind="forget_memory", target_id=other.id, expected_version=1),
                    "external-forget-other",
                )
            elif change == "revise":
                self.memories.revise(
                    self.identity(),
                    self.tenant,
                    other.id,
                    MemoryRevision(
                        replacement=MemoryCreate.model_validate(
                            {**self.body, "value": "OTHER_A_REVISED"}
                        )
                    ),
                    "external-revise-other",
                    '"1"',
                )
            else:
                self.clock.advance(2)
            job = Job.model_validate(
                self.store.data[
                    f"tenants/{self.tenant}/jobs/{job_id(self.tenant, 'run_turn', run)}"
                ]
            )
            with self.assertRaises(PolicyDenied):
                self.jobs.store.run(lambda tx: self.handler.state.check(tx, job))

        objects.before = invalidate
        self.execute(run)
        # The worker marks the stale job obsolete; the existing public projection
        # converges its run status without releasing buffered output.
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        saved = self.store.data[path]
        self.assertTrue(before)
        self.assertEqual(saved["status"], "failed")
        self.assertNotIn("forget_operation_id", saved)
        self.assertNotIn("final_message_id", saved)
        self.assertEqual(saved["memory_records"], before["memory_records"])
        self.assertEqual(saved.get("tool_bindings", []), before.get("tool_bindings", []))
        self.assertEqual(saved.get("continuation", []), before.get("continuation", []))
        self.assertFalse(self.store.data[f"tenants/{self.tenant}/facts/{target.id}"]["deleted"])
        self.assertEqual(len(objects.data), 1)  # Unaccepted intent survives for recovery.
        intent = next(iter(objects.data.values()))
        operation = DeletionIntent.model_validate_json(intent).operation_id
        self.assertNotIn(f"tenants/{self.tenant}/operations/{operation}", self.store.data)
        self.assertNotIn(
            f"tenants/{self.tenant}/memory_forget_effects/{operation}", self.store.data
        )
        self.assertIn(
            f"tenants/{self.tenant}/memory_forget_reservations/{target.id}", self.store.data
        )
        self.assertEqual(len(self.model.requests), 2 if search else 1)
        if search:
            self.assertTrue(saved["tool_bindings"])
        if change == "forget":
            self.assertTrue(self.store.data[f"tenants/{self.tenant}/facts/{other.id}"]["deleted"])
        elif change == "revise":
            self.assertEqual(
                self.store.data[f"tenants/{self.tenant}/facts/{other.id}"]["status"], "superseded"
            )

    def test_external_forget_during_ledger_io_blocks_target_commit_and_terminal(self) -> None:
        self.external_change("forget")

    def test_external_revision_during_ledger_io_is_not_an_atomic_self_revision(self) -> None:
        self.external_change("revise")

    def test_other_memory_expiry_during_ledger_io_blocks_target_commit(self) -> None:
        self.external_change("expire")

    def test_external_forget_preserves_invalidated_search_binding_as_evidence(self) -> None:
        self.external_change("forget", search=True)

    def test_searched_target_self_forget_replays_same_effect_and_keeps_prefix(self) -> None:
        target = self.memory(value="TARGET_B")
        self.memory(value="STILL_VALID_A")
        objects = self.configure_forget(target.id, search=True)
        conversation = self.conversation()
        run = ResourceId(
            self.turn(conversation, text=f"Forget memory {target.id}").json()["run_id"]
        )
        original: Callable[..., Any] = ForgetMemories.forget
        replays: list[ResourceId] = []
        prefixes: list[str] = []
        objects.before = lambda name: prefixes.append(
            self.store.data[f"tenants/{self.tenant}/runs/{run}"].get("streamed_text", "")
        )

        def repeat(service: ForgetMemories, *args: Any, **kwargs: Any) -> Any:
            first = original(service, *args, **kwargs)
            # Exercise a retry after the atomic self-effect has removed its target
            # and rebound this still-live run, before the handler response is released.
            replay = original(service, *args, **kwargs)
            self.assertEqual(first, replay)
            replays.append(first.operation_id)
            return replay

        with patch.object(ForgetMemories, "forget", repeat):
            self.execute(run)
        self.assertEqual(len(replays), 1)
        self.assertEqual(len(objects.data), 1)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(saved["forget_operation_id"], replays[0])
        self.assertEqual(saved["memory_records"], [])
        self.assertEqual(saved["tool_bindings"], [])
        self.assertEqual(saved["continuation"], [])
        message = self.store.data[
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}"
        ]
        self.assertTrue(prefixes[0])
        self.assertTrue(message["text"].startswith(prefixes[0]))
        self.assertIn("消去処理を待っています", message["text"])
        self.assertTrue(message["suppress_reuse"])

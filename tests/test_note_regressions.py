"""APP-01 R1: actual note producers, source forget/export and atomic update completion."""

import copy
import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import (
    Completed,
    ModelMessage,
    ModelResponse,
    TextDelta,
    TextPart,
    ToolCall,
)
from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.repositories.notes import NoteRequests
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts import ResourceId
from brainoid_jobs import RetryableError
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.export_manifest import KIND, ExportCreate
from brainoid_memory.exports import Exports
from brainoid_memory.forget import ForgetCreate, operation_path
from brainoid_memory.notes import NoteCreate, NotePatch, note_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.notes import NotesRuntime
from brainoid_worker.run_turn import RunTurn
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import GraphStore
from fakes.purge import FakePurgePages
from fakes.run_confirmation import MultiStageModel
from test_notes import NOTE, NotesWorkerFixture
from test_purge_history import PurgeHistoryFixture


class EchoNotes(MultiStageModel):
    async def stream(self, request: Any, *, control: Any) -> Any:
        replies = [m for m in request.messages if m.role == "tool"]
        if not replies:
            async for event in super().stream(request, control=control):
                yield event
            return
        self.requests.append(request)
        text = replies[-1].parts[0].result["result"]["notes"][0]["body"]
        yield TextDelta(text=text)
        yield ModelResponse(message=ModelMessage(role="assistant", parts=(TextPart(text=text),)))
        yield Completed()


class NoteSourceForgetTests(PurgeHistoryFixture):
    def note(self, **changes: Any) -> Any:
        return NoteRequests(self.memories).create(
            self.identity(),
            self.tenant,
            NoteCreate.model_validate({**NOTE, **changes}),
            "source-note-" + str(uuid4()),
        )

    def export_payload(self) -> dict[str, Any]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "objects"
        objects = LocalExportObjects(directory, b"x" * 32, now=self.clock.now)
        exports = Exports(
            self.memories.jobs,
            objects,
            FakePurgePages(cast(GraphStore, self.store)),
            CanonicalSources(SavedMessages()),
            now=self.clock.now,
        )
        requests = ExportRequests(exports, self.tenants)
        operation = requests.create(
            self.identity(),
            self.tenant,
            ExportCreate(kind="export", scope="visible_tenant_data"),
            "source-export-after-001",
        )
        delivery = exports.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, KIND, operation.operation_id)
        )
        assert delivery is not None
        Worker(exports.jobs, LocalCaller(), {KIND: ExportData(exports)}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        self.assertEqual(
            self.store.data[operation_path(self.tenant, operation.operation_id)]["operation"][
                "status"
            ],
            "succeeded",
        )
        with zipfile.ZipFile(
            io.BytesIO(requests.download(self.identity(), self.tenant, operation.operation_id))
        ) as archive:
            payload: dict[str, Any] = json.loads(archive.read("data.json"))
            return payload

    def source_case(self, policy: Literal["suppress_reuse", "delete_source"]) -> None:
        note = self.note(title="Selected", body="R1_NOTE_ECHO")
        other = self.note(title="Independent", body="INDEPENDENT_NOTE")
        unrelated_fact = self.memory(value="INDEPENDENT_FACT")
        calls = (
            ToolCall(call_key="list", name="notes.list", arguments_json='{"query":"Selected"}'),
        )
        self.configure_calls(calls)
        self.model = EchoNotes(calls)  # type: ignore[assignment]
        self.handler = RunTurn(self.jobs, self.model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text="List Selected notes").json()["run_id"])
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        path = f"tenants/{self.tenant}/runs/{run}"
        before_run = copy.deepcopy(self.store.data[path])
        before_job = copy.deepcopy(
            self.store.data[f"tenants/{self.tenant}/jobs/{job_id(self.tenant, 'run_turn', run)}"]
        )
        before_note = copy.deepcopy(self.store.data[note_path(self.tenant, note.id)])
        final = f"tenants/{self.tenant}/conversations/{conversation}/messages/{before_run['final_message_id']}"
        self.assertEqual(self.store.data[final]["text"], "FIXTURE_stage_0.R1_NOTE_ECHO")
        originals = {
            p: copy.deepcopy(d)
            for p, d in self.store.data.items()
            if "/messages/" in p and d.get("role") == "user"
        }
        fact = self.memory(source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}])
        same_source_fact = self.memory(
            value="SECOND_FACT",
            source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}],
        )
        ledger = DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(
                kind="forget_memory", target_id=fact.id, expected_version=1, source_policy=policy
            ),
            "source-forget-001",
        )
        intent = ledger.get(operation.operation_id)
        assert intent is not None
        immutable = intent.model_dump_json()
        self.assertNotIn('"kind":"forget_memory"', immutable)
        self.bind_purge(ledger, operation.operation_id)
        self.finish_purge()
        self.assertEqual(ledger.get(operation.operation_id).model_dump_json(), immutable)  # type: ignore[union-attr]
        self.assertNotIn(final, self.store.data)
        self.assertEqual(
            note_path(self.tenant, note.id) in self.store.data, policy == "suppress_reuse"
        )
        if policy == "suppress_reuse":
            self.assertEqual(
                self.store.data[note_path(self.tenant, note.id)]["body"], "R1_NOTE_ECHO"
            )
        self.assertEqual(
            self.store.data[note_path(self.tenant, other.id)]["body"], "INDEPENDENT_NOTE"
        )
        self.assertIn(f"tenants/{self.tenant}/facts/{unrelated_fact.id}", self.store.data)
        self.assertIn(f"tenants/{self.tenant}/facts/{same_source_fact.id}", self.store.data)
        for p, doc in originals.items():
            self.assertEqual(self.store.data[p], doc)
        remaining = [p for p, d in self.store.data.items() if "R1_NOTE_ECHO" in repr(d)]
        self.assertEqual(
            remaining, [note_path(self.tenant, note.id)] if policy == "suppress_reuse" else []
        )
        kept = self.store.data[path]["purge_dependencies"]
        self.assertIn({"note_id": note.id, "version": 1}, kept["notes"])
        self.assertNotIn("R1_NOTE_ECHO", json.dumps(kept))
        payload = self.export_payload()
        self.assertFalse(
            [
                r
                for r in payload["records"]
                if r["kind"] == "messages" and "R1_NOTE_ECHO" in str(r["data"].get("text", ""))
            ]
        )
        self.assertIn("INDEPENDENT_NOTE", json.dumps(payload))
        # Restoring canonical bodies cannot resurrect the accepted source into a model.
        self.store.data[note_path(self.tenant, note.id)] = before_note
        self.store.data[path] = before_run
        job_path = f"tenants/{self.tenant}/jobs/{job_id(self.tenant, 'run_turn', run)}"
        self.store.data[job_path] = {
            **before_job,
            "status": "pending",
            "lease_expires_at": None,
            "dispatch_after": before_job["created_at"],
        }
        calls_before = len(self.model.requests)
        self.execute(run)
        self.assertEqual(len(self.model.requests), calls_before)

    def test_delete_source_erases_note_copies_before_new_export(self) -> None:
        self.source_case("delete_source")

    def test_suppress_reuse_keeps_original_and_independent_sources_only(self) -> None:
        self.source_case("suppress_reuse")

    def test_tool_created_note_effect_removed_but_explicit_user_source_retained(self) -> None:
        args = {**NOTE, "body": "TOOL_NOTE_SOURCE"}
        self.configure_calls(
            (ToolCall(call_key="create", name="notes.create", arguments_json=json.dumps(args)),)
        )
        conv = self.conversation()
        run = ResourceId(
            self.turn(conv, text="Save shared note 買い物: TOOL_NOTE_SOURCE").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        effects = [d for p, d in self.store.data.items() if "/note_effects/" in p]
        self.assertEqual(len(effects), 1)
        note = ResourceId(effects[0]["target_id"])
        fact = self.memory(source_refs=[{"kind": "note", "resource_id": note, "version": 1}])
        ledger = DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        op = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(
                kind="forget_memory",
                target_id=fact.id,
                expected_version=1,
                source_policy="delete_source",
            ),
            "effect-source-forget",
        )
        self.bind_purge(ledger, op.operation_id)
        self.finish_purge()
        remaining = [d for d in self.store.data.values() if "TOOL_NOTE_SOURCE" in repr(d)]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["role"], "user")

    def test_self_forget_retains_note_evidence_after_inputs_are_cleared(self) -> None:
        note = self.note(body="SELF_FORGET_NOTE")
        fact = self.memory(source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}])
        self.configure_calls(
            (
                ToolCall(call_key="list", name="notes.list", arguments_json="{}"),
                ToolCall(
                    call_key="forget",
                    name="memory.forget",
                    arguments_json=json.dumps(
                        {
                            "memory_id": fact.id,
                            "expected_version": 1,
                            "source_policy": "delete_source",
                        }
                    ),
                ),
            )
        )
        ledger = DeletionLedger(MemoryObjects())
        self.handler.memory.deletion_ledger = ledger
        conv = self.conversation()
        run = ResourceId(
            self.turn(conv, text=f"Forget memory {fact.id} and delete its source").json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved["tool_bindings"], [])
        self.assertIn({"note_id": note.id, "version": 1}, saved["purge_dependencies"]["notes"])
        self.bind_purge(ledger, ResourceId(saved["forget_operation_id"]))
        self.finish_purge()
        self.assertNotIn("SELF_FORGET_NOTE", repr(self.store.data))

    def retired_evidence_case(self, *, missing_receipts: bool) -> None:
        note = self.note(body="RETIRED_NOTE_INPUT")
        changes = {"body": "UPDATED_NOTE_INPUT"}
        self.configure_calls(
            (
                ToolCall(call_key="list", name="notes.list", arguments_json="{}"),
                ToolCall(
                    call_key="update",
                    name="notes.update",
                    arguments_json=json.dumps(
                        {"note_id": note.id, "expected_version": 1, "changes": changes}
                    ),
                ),
            )
        )
        run = ResourceId(
            self.turn(
                self.conversation(),
                text=f"Update note {note.id}: " + json.dumps(changes, sort_keys=True),
            ).json()["run_id"]
        )
        self.execute(run)
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        # A restore/corruption cannot turn a lost input inventory into a clean run.
        del saved["purge_dependencies"]
        if missing_receipts:
            for call in saved["tool_call_ids"]:
                del self.store.data[f"tenants/{self.tenant}/tool_executions/{call}"]
        fact = self.memory(source_refs=[{"kind": "note", "resource_id": note.id, "version": 2}])
        ledger = DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        op = self.memories.forget(
            self.identity(),
            self.tenant,
            ForgetCreate(
                kind="forget_memory",
                target_id=fact.id,
                expected_version=1,
                source_policy="delete_source",
            ),
            "retired-note-forget",
        )
        self.bind_purge(ledger, op.operation_id)
        if missing_receipts:
            with self.assertRaises(RetryableError):
                self.purge.run(self.purge_execution)
            self.assertEqual(
                self.store.data[self.op_path]["purge"]["retry_reason"], "legacy_provenance_missing"
            )
            self.assertNotEqual(self.store.data[self.op_path]["operation"]["status"], "succeeded")
        else:
            self.finish_purge()
            self.assertNotIn("RETIRED_NOTE_INPUT", repr(self.store.data))

    def test_retired_note_provenance_recovers_from_bound_receipts(self) -> None:
        self.retired_evidence_case(missing_receipts=False)

    def test_lost_retired_note_provenance_refuses_purge_success(self) -> None:
        self.retired_evidence_case(missing_receipts=True)


class NoteUpdateCompletionTests(NotesWorkerFixture):
    def prepare_update(self, note: Any, *, listing: bool = True) -> ResourceId:
        changes = {"body": "Alex updated"}
        calls = [
            ToolCall(
                call_key="update",
                name="notes.update",
                arguments_json=json.dumps(
                    {"note_id": note.id, "expected_version": 1, "changes": changes}
                ),
            )
        ]
        if listing:
            calls.insert(0, ToolCall(call_key="list", name="notes.list", arguments_json="{}"))
        self.configure_calls(tuple(calls))
        return ResourceId(
            self.turn(
                self.conversation(),
                text=f"Update note {note.id}: " + json.dumps(changes, sort_keys=True),
            ).json()["run_id"]
        )

    def assert_updated(self, run: ResourceId, note: Any) -> dict[str, Any]:
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[note_path(self.tenant, note.id)]["version"], 2)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        message = self.store.data[
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
        ]
        self.assertEqual(message["text"], NotesRuntime.UPDATE_ACK)
        self.assertEqual(message["tool_bindings"], [])
        self.assertEqual(message["memory_records"], [])
        self.assertIsNone(message["model_messages"])
        return saved

    def test_list_multiple_notes_and_note_sourced_memory_retired_atomically(self) -> None:
        note, other = self.note(body="OLD_NOTE_BODY"), self.note(body="OTHER_NOTE_BODY")
        fact = self.memory(
            value="OLD_NOTE_FACT",
            source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}],
        )
        run = self.prepare_update(note)
        self.execute(run)
        saved = self.assert_updated(run, note)
        self.assertEqual(len(self.model.requests), 2)
        self.assertIn("OLD_NOTE_FACT", self.model.requests[0].model_dump_json())
        self.assertIn("OTHER_NOTE_BODY", self.model.requests[1].model_dump_json())
        self.assertEqual(saved["memory_records"], [])
        self.assertEqual(saved["continuation"], [])
        dependencies = saved["purge_dependencies"]
        self.assertTrue(dependencies["complete"])
        self.assertIn(fact.id, [r["memory_id"] for r in dependencies["facts"]])
        self.assertEqual({n["note_id"] for n in dependencies["notes"]}, {note.id, other.id})
        self.assertNotIn("OLD_NOTE", json.dumps(dependencies))

    def test_response_loss_and_read_only_reconcile_do_not_repeat_update(self) -> None:
        note = self.note()
        run = self.prepare_update(note)
        execute = NotesRuntime.execute_notes
        reconciled = []

        async def lost(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "notes.update":
                recovered = await execute(runtime, name, invocation, reconcile=True, **kwargs)
                self.assertEqual(recovered, result)
                reconciled.append(recovered)
                raise RuntimeError("synthetic update response loss")
            return result

        with patch.object(NotesRuntime, "execute_notes", lost):
            self.execute(run)
        self.assert_updated(run, note)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(len(self.model.requests), 2)
        self.assertEqual(sum("/note_effects/" in p for p in self.store.data), 1)

    def test_lost_atomic_commit_response_retries_ack_without_model_or_update(self) -> None:
        note = self.note()
        run = self.prepare_update(note)
        thrown: list[bool] = []

        original = self.store.run

        def lost(work: Any) -> Any:
            result = original(work)
            if self.store.data[note_path(self.tenant, note.id)]["version"] == 2 and not thrown:
                thrown.append(True)
                raise PersistenceUnavailable()
            return result

        with patch.object(self.store, "run", lost), self.assertRaises(PersistenceUnavailable):
            self.execute(run)
        self.assertTrue(thrown)
        self.assertEqual(self.store.data[note_path(self.tenant, note.id)]["version"], 2)
        calls = len(self.model.requests)
        self.clock.advance(120)
        self.execute(run)
        self.assert_updated(run, note)
        self.assertEqual(len(self.model.requests), calls)
        self.assertEqual(sum("/note_effects/" in p for p in self.store.data), 1)

    def test_external_change_to_other_list_item_blocks_update_before_commit(self) -> None:
        note, other = self.note(), self.note(body="OTHER_NOTE")
        run = self.prepare_update(note)
        mutate = self.handler.memory.notes.service.mutate

        def concurrent(*args: Any, **kwargs: Any) -> Any:
            NoteRequests(self.memories).update(
                self.identity(),
                self.tenant,
                other.id,
                NotePatch(body="Externally edited"),
                "external-other-note",
                '"1"',
            )
            return mutate(*args, **kwargs)

        with patch.object(self.handler.memory.notes.service, "mutate", concurrent):
            self.execute(run)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[note_path(self.tenant, note.id)]["version"], 1)
        self.assertEqual(self.store.data[note_path(self.tenant, other.id)]["version"], 2)

    def post_commit_denied(self, change: str) -> None:
        note = self.note()
        run = self.prepare_update(note)
        execute = NotesRuntime.execute_notes

        async def changed(runtime: Any, name: str, invocation: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, name, invocation, **kwargs)
            if name == "notes.update":
                if change == "edit":
                    NoteRequests(self.memories).update(
                        self.identity(),
                        self.tenant,
                        note.id,
                        NotePatch(body="External v3"),
                        "external-target-note",
                        '"2"',
                    )
                elif change == "expire":
                    self.clock.advance(121)
                else:
                    for p, d in self.store.data.items():
                        if "/memberships/" in p and d.get("user_id") == self.identity().user_id:
                            d["status"] = "removed"
            return result

        with patch.object(NotesRuntime, "execute_notes", changed):
            self.execute(run)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertNotEqual(saved["status"], "completed")
        self.assertEqual(len(self.model.requests), 2)

    def test_post_commit_external_edit_denies_ack(self) -> None:
        self.post_commit_denied("edit")

    def test_post_commit_revocation_denies_ack(self) -> None:
        self.post_commit_denied("revoke")

    def test_post_commit_expiry_denies_ack(self) -> None:
        self.post_commit_denied("expire")

    def test_terminal_proof_cannot_be_reused_by_another_run(self) -> None:
        note = self.note()
        run = self.prepare_update(note)
        self.execute(run)
        saved = self.assert_updated(run, note)
        second = ResourceId(
            self.turn(self.conversation(), key="other-terminal-run", text="List notes").json()[
                "run_id"
            ]
        )
        other = self.store.data[f"tenants/{self.tenant}/runs/{second}"]
        other["note_update"] = copy.deepcopy(saved["note_update"])
        other["tool_call_ids"] = list(saved["tool_call_ids"])
        before = len(self.model.requests)
        self.execute(second)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), before)

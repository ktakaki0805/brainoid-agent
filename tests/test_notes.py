"""APP-01 / T-001 T-012: shared HTTP/tool persistence and current note boundaries."""

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_agent_core.types import ToolCall
from brainoid_api.errors import APIError
from brainoid_api.repositories.notes import NoteRequests
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import tombstone_path
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import DeleteNote, operation_path
from brainoid_memory.notes import NoteCreate, Notes, note_path
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_worker.purge_authority import KIND
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import FACT, NOW, OTHER, SOURCE, TENANT, USER, context, fact_values
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_exports import ExportFixture
from test_memories import MemoryFixture
from test_memory_purge import PurgeFixture
from test_purge_history import PurgeHistoryFixture
from test_scoped_retrieval import MemoryWorkerFixture

NOTE = {
    "title": "買い物",
    "body": "Milk and tea",
    "visibility": "shared",
    "display_policy": "shared_display",
}


class NotesFixture(MemoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = f"/v1/tenants/{TENANT}/notes"
        self.notes = NoteRequests(self.memories)
        self.objects = MemoryObjects()
        self.memories.deletion_ledger = DeletionLedger(self.objects)

    def create_note(self, key: str = "note-create-key-001", **changes: Any) -> Any:
        return self.client.post(
            self.url, json={**NOTE, **changes}, headers={"Idempotency-Key": key}
        )

    def update_note(
        self, identity: str, key: str = "note-update-key-001", version: int = 1, **changes: Any
    ) -> Any:
        return self.client.patch(
            self.url + "/" + identity,
            json=changes or {"body": "Tea"},
            headers={"Idempotency-Key": key, "If-Match": f'"{version}"'},
        )

    def delete_note(self, identity: str, key: str = "note-delete-key-001", **changes: Any) -> Any:
        return self.client.post(
            f"/v1/tenants/{TENANT}/operations",
            json={
                "kind": "delete_resource",
                "target_type": "note",
                "target_id": identity,
                "expected_version": 1,
                **changes,
            },
            headers={"Idempotency-Key": key},
        )

    def member(self, role: str = "member") -> None:
        self.uid = FirebaseUid("another/user/opaque")
        self.store.data[f"tenants/{TENANT}/memberships/another"] = {
            "tenant_id": TENANT,
            "user_id": self.uid,
            "role": role,
            "status": "active",
            "authorization_version": 1,
        }


class NotesHTTPTests(NotesFixture):
    def test_crud_schema_source_actor_etag_search_cursor_and_replay(self) -> None:
        response = self.create_note()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.headers["etag"], '"1"')
        note = response.json()
        saved = self.store.data[note_path(TENANT, ResourceId(note["id"]))]
        self.assertEqual(
            (
                saved["created_by"],
                saved["updated_by"],
                saved["source_refs"],
                saved["deletion_epoch"],
            ),
            (USER, USER, [], 0),
        )
        self.assertEqual(self.create_note().json(), note)
        self.assertEqual(self.create_note(body="different").status_code, 409)
        second = self.create_note(key="note-second-key-001", title="Coffee").json()
        page = self.client.get(self.url, params={"query": "milk", "limit": 1}).json()
        self.assertEqual(len(page["items"]), 1)
        self.assertIsInstance(page["next_cursor"], str)
        after = self.client.get(
            self.url, params={"query": "milk", "limit": 1, "cursor": page["next_cursor"]}
        ).json()
        self.assertEqual(len(after["items"]), 1)
        self.assertIsNone(after["next_cursor"])
        self.assertEqual(
            {n["id"] for n in page["items"] + after["items"]}, {note["id"], second["id"]}
        )
        self.assertNotEqual(
            self.client.get(
                self.url, params={"query": "coffee", "limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            200,
        )
        spec = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        for name, value in (("Note", note), ("NotePage", after)):
            Draft202012Validator(
                {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
            ).validate(value)
        changed = self.update_note(note["id"])
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.headers["etag"], '"2"')
        self.assertEqual(self.update_note(note["id"]).json(), changed.json())
        self.assertEqual(self.update_note(note["id"], key="stale-note-key-001").status_code, 412)

    def test_private_hidden_shared_creator_or_admin_and_cross_tenant(self) -> None:
        shared = self.create_note().json()["id"]
        private = self.create_note(
            key="private-note-key-01", visibility="private", display_policy="never"
        ).json()["id"]
        self.member()
        self.assertEqual(self.client.get(self.url + "/" + private).status_code, 404)
        self.assertNotIn(private, self.client.get(self.url).text)
        self.assertEqual(self.update_note(private).status_code, 404)
        self.assertEqual(self.delete_note(private).status_code, 404)
        self.assertEqual(self.update_note(shared).status_code, 403)
        self.assertEqual(self.delete_note(shared).status_code, 403)
        self.assertEqual(self.client.get(f"/v1/tenants/{OTHER}/notes/{shared}").status_code, 404)
        self.store.data[f"tenants/{TENANT}/memberships/another"]["role"] = "admin"
        edited = self.update_note(shared)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(
            self.store.data[note_path(TENANT, ResourceId(shared))]["updated_by"], self.uid
        )
        self.assertEqual(self.delete_note(shared, expected_version=2).status_code, 202)
        self.assertEqual(self.client.get(self.url + "/" + private).status_code, 404)

    def test_invalid_patch_scope_unknown_fields_and_untyped_versions(self) -> None:
        note = self.create_note(visibility="private", display_policy="never").json()["id"]
        for body in (
            {},
            {"body": None},
            {"title": ""},
            {"visibility": "shared"},
            {"display_policy": "shared_display"},
        ):
            with self.subTest(body=body):
                result = self.client.patch(
                    self.url + "/" + note,
                    json=body,
                    headers={"Idempotency-Key": "invalid-note-key-01", "If-Match": '"1"'},
                )
                self.assertNotEqual(result.status_code, 200)
        self.assertEqual(self.delete_note(note, expected_version=True).status_code, 422)
        self.assertEqual(self.delete_note(note, target_type="task").status_code, 422)

    def test_delete_replay_hides_api_model_sources_and_stale_restoration(self) -> None:
        note = self.create_note().json()["id"]
        path = note_path(TENANT, ResourceId(note))
        old = copy.deepcopy(self.store.data[path])
        accepted = self.delete_note(note)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(self.delete_note(note).json(), accepted.json())
        self.assertEqual(accepted.json()["kind"], "delete_resource")
        self.assertEqual(
            self.client.get(
                f"/v1/tenants/{TENANT}/operations/{accepted.json()['operation_id']}"
            ).status_code,
            200,
        )
        for restore in (False, True):
            if restore:
                self.store.data[path] = old
            self.assertEqual(self.client.get(self.url + "/" + note).status_code, 404)
            self.assertNotIn(note, self.client.get(self.url).text)
            self.assertNotEqual(self.create_note().status_code, 201)
            with self.assertRaises(PolicyDenied):
                self.store.run(
                    lambda tx: Notes.get(
                        self.memories._open(tx, self.identity(), TENANT, None),
                        ResourceId(note),
                        for_model=True,
                    )
                )

    def test_ledger_failure_pending_lock_recovery_and_restore(self) -> None:
        note = self.create_note().json()["id"]
        before = copy.deepcopy(self.store.data)
        with patch.object(self.objects, "create", side_effect=LedgerError()):
            self.assertEqual(self.delete_note(note).status_code, 503)
        self.assertEqual(self.update_note(note).status_code, 412)
        accepted = self.delete_note(note)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        immutable = copy.deepcopy(self.objects.data)
        self.store.data = before
        self.now += timedelta(seconds=1)
        replay = self.delete_note(note)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["operation_id"], accepted.json()["operation_id"])
        self.assertEqual(replay.json()["created_at"], accepted.json()["created_at"])
        self.assertEqual(self.objects.data, immutable)
        self.assertEqual(self.store.data[note_path(TENANT, ResourceId(note))]["version"], 2)
        self.assertEqual(sum("/jobs/" in p for p in self.store.data), 1)

    def test_revocation_slow_ledger_and_independent_ledger_failure_are_denied(self) -> None:
        note = self.create_note().json()["id"]

        def expire(name: str) -> None:
            self.now += timedelta(hours=2)

        self.objects.before = expire
        self.assertNotEqual(self.delete_note(note).status_code, 202)
        self.assertFalse(self.store.data[note_path(TENANT, ResourceId(note))]["deleted"])
        self.objects.before = lambda name: None
        self.now = NOW
        with patch.object(self.ledger, "latest", side_effect=PolicyUnavailable()):
            self.assertEqual(self.client.get(self.url + "/" + note).status_code, 503)

    def test_commit_response_loss_recovers_without_second_note(self) -> None:
        lost: list[bool] = []

        def response_loss() -> None:
            if (
                any(
                    "/notes/" in p and d.get("title") == NOTE["title"]
                    for p, d in self.store.data.items()
                )
                and not lost
            ):
                lost.append(True)
                raise RuntimeError("synthetic loss")

        self.store.after_commit = response_loss
        with self.assertRaises(RuntimeError):
            self.notes.create(
                self.identity(), TENANT, NoteCreate.model_validate(NOTE), "lost-note-create-key"
            )
        self.store.after_commit = lambda: None
        result = self.notes.create(
            self.identity(), TENANT, NoteCreate.model_validate(NOTE), "lost-note-create-key"
        )
        self.assertEqual(result.version, 1)
        self.assertEqual(
            sum(
                d.get("title") == NOTE["title"]
                for p, d in self.store.data.items()
                if "/notes/" in p
            ),
            1,
        )

    def test_unknown_device_write_requires_current_notes_session(self) -> None:
        from brainoid_api.routes.agents import reader_identity
        from brainoid_api.services.private_sessions import PrivateSessionCreate, PrivateSessions

        device = self.device()
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + device.access_token,
            "Idempotency-Key": "device-note-create-01",
        }
        self.assertEqual(self.client.post(self.url, json=NOTE, headers=headers).status_code, 403)
        session = PrivateSessions(
            self.store, self.tenants, self.ledger, self.credentials, now=lambda: self.now
        ).start(
            self.identity(),
            TENANT,
            device.device_id,
            PrivateSessionCreate(allowed_scopes=["notes"], acknowledge_shared_space=True),
            "note-session-create",
            "note-session-request",
        )
        headers["X-Private-Session"] = session.id
        response = self.client.post(self.url, json=NOTE, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        note = self.store.data[note_path(TENANT, ResourceId(response.json()["id"]))]
        self.assertEqual(
            (note["created_by"], note["created_via_device_id"]), (USER, device.device_id)
        )
        self.now += timedelta(seconds=61)
        self.assertNotEqual(self.client.get(self.url, headers=headers).status_code, 200)

    def test_last_read_and_commit_response_expiry_do_not_release_note(self) -> None:
        note = self.create_note().json()["id"]
        self.store.after_commit = lambda: setattr(self, "now", NOW + timedelta(hours=2))
        with self.assertRaises(APIError):
            self.notes.get(self.identity(), TENANT, ResourceId(note))


class NotesWorkerFixture(MemoryWorkerFixture):
    def note(self, **changes: Any) -> Any:
        return NoteRequests(self.memories).create(
            self.identity(),
            self.tenant,
            NoteCreate.model_validate({**NOTE, **changes}),
            "note-key-" + str(uuid4()),
        )

    def call(self, name: str, arguments: dict[str, Any], text: str) -> ResourceId:
        self.configure_calls(
            (ToolCall(call_key="note-call", name=name, arguments_json=json.dumps(arguments)),)
        )
        self.handler.memory.deletion_ledger = DeletionLedger(MemoryObjects())
        conversation = self.conversation()
        run = ResourceId(self.turn(conversation, text=text).json()["run_id"])
        self.execute(run)
        return run


class NotesWorkerTests(NotesWorkerFixture):
    def test_actual_create_tool_matches_api_storage_and_reuses_current_result(self) -> None:
        api = self.note()
        run = self.call("notes.create", NOTE, "Save shared note 買い物: Milk and tea")
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        notes = [
            d
            for p, d in self.store.data.items()
            if p.startswith(f"tenants/{self.tenant}/notes/") and "updated_by" in d
        ]
        self.assertEqual(len(notes), 2)
        api_doc = self.store.data[note_path(self.tenant, api.id)]
        tool_doc = next(d for d in notes if d["id"] != api.id)
        for field in (
            "title",
            "body",
            "visibility",
            "display_policy",
            "created_by",
            "updated_by",
            "owner_user_id",
            "source_refs",
            "deletion_epoch",
        ):
            self.assertEqual(api_doc[field], tool_doc[field])
        self.assertEqual(len(self.model.requests), 2)
        self.assertIn("Milk and tea", self.model.requests[-1].model_dump_json())

    def test_create_mismatched_body_scope_negation_and_derived_user_cannot_write(self) -> None:
        for text in (
            "Save shared note 買い物: different",
            "Do not Save shared note 買い物: Milk and tea",
            "Save private note 買い物: Milk and tea",
        ):
            with self.subTest(text=text):
                self.call("notes.create", NOTE, text)
                self.assertFalse(
                    any("/notes/" in p and "updated_by" in d for p, d in self.store.data.items())
                )

    def test_actual_update_tool_uses_shared_service_and_stale_version_rejected(self) -> None:
        note = self.note()
        changes = {"body": "Updated"}
        text = f"Update note {note.id}: " + json.dumps(changes, sort_keys=True)
        run = self.call(
            "notes.update", {"note_id": note.id, "expected_version": 1, "changes": changes}, text
        )
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(self.store.data[note_path(self.tenant, note.id)]["version"], 2)
        again = self.call(
            "notes.update", {"note_id": note.id, "expected_version": 1, "changes": changes}, text
        )
        self.assertNotEqual(self.get_run(again).json()["status"], "completed")
        self.assertEqual(self.store.data[note_path(self.tenant, note.id)]["version"], 2)

    def test_list_never_reveals_private_in_shared_conversation_or_suppressed_note(self) -> None:
        self.note(body="VISIBLE_NOTE")
        self.note(body="PRIVATE_NOTE", visibility="private", display_policy="never")
        suppressed = self.note(body="SUPPRESSED_NOTE")
        self.store.data[note_path(self.tenant, suppressed.id)]["suppress_reuse"] = True
        run = self.call("notes.list", {}, "List notes")
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        prompt = self.model.requests[-1].model_dump_json()
        self.assertIn("VISIBLE_NOTE", prompt)
        self.assertNotIn("PRIVATE_NOTE", prompt)
        self.assertNotIn("SUPPRESSED_NOTE", prompt)

    def test_delete_current_memory_source_stops_model_and_clears_old_inputs(self) -> None:
        note = self.note(body="SOURCE_NOTE_SENTINEL")
        self.memory(
            value="SOURCE_NOTE_SENTINEL",
            source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}],
        )
        run = self.call(
            "notes.delete", {"note_id": note.id, "expected_version": 1}, f"Delete note {note.id}"
        )
        self.assertEqual(self.get_run(run).json()["status"], "completed")
        self.assertEqual(len(self.model.requests), 1)
        saved = self.store.data[f"tenants/{self.tenant}/runs/{run}"]
        self.assertEqual(saved["memory_records"], [])
        self.assertEqual(saved["continuation"], [])
        self.assertEqual(saved["tool_bindings"], [])
        self.assertTrue(self.store.data[note_path(self.tenant, note.id)]["deleted"])
        final = self.store.data[
            f"tenants/{self.tenant}/conversations/{saved['conversation_id']}/messages/{saved['final_message_id']}"
        ]
        self.assertIn("消去処理を待っています", final["text"])
        self.assertNotIn("SOURCE_NOTE_SENTINEL", final["text"])


class NotePurgeTests(PurgeFixture):
    def accept_note(
        self, *, include: bool = True, admin: bool = False, private_derived: bool = True
    ) -> ResourceId:
        service = Notes(self.repo._store, now=lambda: self.now)
        note = service.mutate(
            lambda tx: self.repo.facts(tx, context()),
            NoteCreate.model_validate(NOTE),
            "purge-note-create-key",
        )
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}]),
        )
        # A shared-note editor may purge its derived private copy, but cannot read it.
        if private_derived:
            self.store.data[self.fact_path].update(
                visibility="private", owner_user_id="other-private-user", display_policy="never"
            )
        if admin:
            self.store.data[note_path(TENANT, note.id)]["created_by"] = "different-author"
        self.operation = self.acceptor.forget(
            lambda tx: self.repo.facts(tx, context()),
            DeleteNote(
                kind="delete_resource",
                target_type="note",
                target_id=note.id,
                expected_version=1,
                include_derived_memories=include,
            ),
            "purge-note-delete-key",
        )
        self.job_id = job_id(TENANT, KIND, self.operation.operation_id)
        self.op_path = operation_path(TENANT, self.operation.operation_id)
        return note.id

    def test_shared_admin_purges_exact_note_private_derived_fact_vectors_children_and_restore(
        self,
    ) -> None:
        note = self.accept_note(admin=True)
        unrelated = f"tenants/{TENANT}/facts/{SOURCE}"
        self.store.data[unrelated] = {
            **self.store.data[self.fact_path],
            "id": SOURCE,
            "source_refs": [],
        }
        vector = f"tenants/{TENANT}/memory_vectors_{'a' * 32}/{FACT}"
        self.store.data[vector] = {"id": FACT, "tenant_id": TENANT}
        self.store.data[self.fact_path + "/parts/a"] = {"tenant_id": TENANT, "text": "DERIVED"}
        old_fact = copy.deepcopy(self.store.data[self.fact_path])
        self.drain()
        self.assertNotIn(note_path(TENANT, note), self.store.data)
        self.assertNotIn(self.fact_path, self.store.data)
        self.assertNotIn(vector, self.store.data)
        self.assertIn(unrelated, self.store.data)
        self.assertIn(tombstone_path(TENANT, "facts", FACT), self.store.data)
        self.store.data[self.fact_path] = old_fact
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(), FACT)

    def test_include_false_preserves_fact_but_not_source_reuse(self) -> None:
        note = self.accept_note(include=False, private_derived=False)
        self.drain()
        self.assertIn(self.fact_path, self.store.data)
        self.assertNotIn(note_path(TENANT, note), self.store.data)
        self.assertFalse(self.store.data[self.fact_path]["deleted"])
        with self.assertRaises(PolicyDenied):
            self.repo.get_fact(context(), FACT)

    def test_note_and_fact_forget_keep_independent_accepted_authority(self) -> None:
        from brainoid_memory.forget import ForgetCreate

        service = Notes(self.repo._store, now=lambda: self.now)
        note = service.mutate(
            lambda tx: self.repo.facts(tx, context()),
            NoteCreate.model_validate(NOTE),
            "overlap-note-create",
        )
        self.repo.create_fact(
            context(),
            FACT,
            fact_values(source_refs=[{"kind": "note", "resource_id": note.id, "version": 1}]),
        )
        original = self.acceptor.forget(
            lambda tx: self.repo.facts(tx, context()),
            ForgetCreate(kind="forget_memory", target_id=FACT, expected_version=1),
            "overlap-fact-forget",
        )
        marker = copy.deepcopy(self.store.data[tombstone_path(TENANT, "facts", FACT)])
        self.operation = self.acceptor.forget(
            lambda tx: self.repo.facts(tx, context()),
            DeleteNote(
                kind="delete_resource", target_type="note", target_id=note.id, expected_version=2
            ),
            "overlap-note-delete",
        )
        self.job_id = job_id(TENANT, KIND, self.operation.operation_id)
        self.op_path = operation_path(TENANT, self.operation.operation_id)
        self.drain()
        self.assertEqual(self.store.data[tombstone_path(TENANT, "facts", FACT)], marker)
        self.operation = original
        self.job_id = job_id(TENANT, KIND, original.operation_id)
        self.op_path = operation_path(TENANT, original.operation_id)
        self.drain()


class NoteExportTests(ExportFixture):
    def test_real_note_projection_and_delete_invalidation(self) -> None:
        note = NoteRequests(self.memories).create(
            self.identity(), TENANT, NoteCreate.model_validate(NOTE), "export-real-note-key"
        )
        identity = self.ready()
        self.assertIn("Milk and tea", json.dumps(self.archive(identity)))
        operation = self.memories.forget(
            self.identity(),
            TENANT,
            DeleteNote(
                kind="delete_resource", target_type="note", target_id=note.id, expected_version=1
            ),
            "export-note-delete-key",
        )
        self.assertNotEqual(self.download(identity).status_code, 200)
        intent = (
            self.memories.deletion_ledger.get(operation.operation_id)
            if self.memories.deletion_ledger
            else None
        )
        assert intent is not None
        self.exports.invalidate(intent, key=f"{operation.operation_id}:export:v1")
        self.erased(identity)


class NoteHistoryTests(PurgeHistoryFixture):
    def test_note_tool_history_purged_after_source_deletion(self) -> None:
        note = NoteRequests(self.memories).create(
            self.identity(), self.tenant, NoteCreate.model_validate(NOTE), "history-note-create"
        )
        self.configure_calls((ToolCall(call_key="list", name="notes.list", arguments_json="{}"),))
        conversation = self.conversation()
        first = ResourceId(self.turn(conversation, text="List notes").json()["run_id"])
        self.execute(first)
        self.assertEqual(self.get_run(first).json()["status"], "completed")
        saved = copy.deepcopy(self.store.data[f"tenants/{self.tenant}/runs/{first}"])
        ledger = DeletionLedger(MemoryObjects())
        self.memories.deletion_ledger = ledger
        operation = self.memories.forget(
            self.identity(),
            self.tenant,
            DeleteNote(
                kind="delete_resource", target_type="note", target_id=note.id, expected_version=1
            ),
            "history-note-delete",
        )
        # The actual persisted tool result is denied immediately, before physical purge.
        second = ResourceId(
            self.turn(conversation, key="history-after-delete", text="Continue").json()["run_id"]
        )
        before = len(self.model.requests)
        self.execute(second)
        self.assertEqual(len(self.model.requests), before)
        self.assertNotEqual(self.get_run(second).json()["status"], "completed")
        self.bind_purge(ledger, operation.operation_id)
        self.finish_purge()
        self.assertNotIn(
            f"tenants/{self.tenant}/conversations/{conversation}/messages/{saved['final_message_id']}",
            self.store.data,
        )
        self.assertEqual(
            self.store.data[f"tenants/{self.tenant}/runs/{first}"]["error_code"], "source_deleted"
        )
        self.assertNotIn("Milk and tea", repr(self.store.data))


class NotesReleaseTests(NotesWorkerFixture):
    def test_list_result_changed_after_handler_does_not_reach_second_model_call(self) -> None:
        note = self.note()
        from brainoid_worker.notes import NotesRuntime

        execute = NotesRuntime.execute_notes

        async def changed(runtime: Any, *args: Any, **kwargs: Any) -> Any:
            result = await execute(runtime, *args, **kwargs)
            self.store.data[note_path(self.tenant, note.id)]["version"] += 1
            return result

        with patch.object(NotesRuntime, "execute_notes", changed):
            run = self.call("notes.list", {}, "List notes")
        self.assertEqual(len(self.model.requests), 1)
        self.assertNotEqual(self.get_run(run).json()["status"], "completed")

    def test_create_commit_response_loss_returns_unknown_and_preserves_one_effect(self) -> None:
        from brainoid_worker.notes import NotesRuntime

        execute = NotesRuntime.execute_notes

        async def lost(runtime: Any, *args: Any, **kwargs: Any) -> Any:
            await execute(runtime, *args, **kwargs)
            raise RuntimeError("synthetic response loss")

        with patch.object(NotesRuntime, "execute_notes", lost):
            run = self.call("notes.create", NOTE, "Save shared note 買い物: Milk and tea")
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(self.get_run(run).json()["status"], "failed")
        self.assertEqual(sum("/note_effects/" in p for p in self.store.data), 1)
        self.assertEqual(
            sum(
                "/notes/" in p and d.get("title") == NOTE["title"]
                for p, d in self.store.data.items()
            ),
            1,
        )

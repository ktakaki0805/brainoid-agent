"""APP-01 official Firestore: authorized editor races and exact-note purge."""

import json
from functools import partial
from typing import Any
from unittest.mock import patch

from brainoid_agent_core.types import ToolCall
from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.pagination import Cursors
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.notes import NoteRequests
from brainoid_contracts import DisplayPolicy, FirebaseUid, Visibility
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.forget import DeleteNote, Operation, operation_path
from brainoid_memory.notes import NoteCreate, NotePatch, NoteView, note_path
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_runs.models import TurnCreate
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from brainoid_worker.notes import NotesRuntime
from brainoid_worker.purge_authority import KIND, PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeDispatcher, PurgeResource
from brainoid_worker.run_turn import RunTurn
from fakes.run_confirmation import MultiStageModel
from google.api_core.exceptions import ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture
from test_turn_run_integration import TurnRunIntegrationFixture


class NotesIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        self.notes = NoteRequests(self.memories)
        self.note_body = NoteCreate.model_validate(
            {
                "title": "Emulator note",
                "body": "SYNTHETIC_NOTE",
                "visibility": "shared",
                "display_policy": "shared_display",
            }
        )
        self.memories.deletion_ledger = DeletionLedger(LocalAuthzObjects(self.directory))

    def create_note(self) -> NoteView:
        return self.notes.create(
            self.identity(), self.tenant, self.note_body, "emulator-note-create"
        )

    def test_identical_create_and_two_authorized_editors_compare_and_swap(self) -> None:
        first, second = self.concurrent(self.create_note, self.create_note)
        self.assertIsInstance(first, NoteView)
        self.assertEqual(first, second)
        assert isinstance(first, NoteView)
        admin = self.identity().model_copy(update={"user_id": FirebaseUid("note/admin/opaque")})
        self.db.document(f"tenants/{self.tenant}/memberships/note-admin").set(
            {
                "tenant_id": self.tenant,
                "user_id": admin.user_id,
                "role": "admin",
                "status": "active",
                "authorization_version": 1,
            }
        )

        def update(identity: AuthenticatedIdentity, key: str) -> NoteView:
            return self.notes.update(
                identity, self.tenant, first.id, NotePatch(body=key), key, '"1"'
            )

        results = self.concurrent(
            partial(update, self.identity(), "note-owner-update-01"),
            partial(update, admin, "note-admin-update-01"),
        )
        self.assertEqual(sum(isinstance(r, NoteView) for r in results), 1)
        self.assertIn(ErrorCode.VERSION_CONFLICT, results)
        self.assertEqual(self.notes.get(self.identity(), self.tenant, first.id).version, 2)

    def test_delete_vs_edit_race_and_retry_purge(self) -> None:
        note = self.create_note()

        def delete() -> Operation:
            return self.memories.forget(
                self.identity(),
                self.tenant,
                DeleteNote(
                    kind="delete_resource",
                    target_type="note",
                    target_id=note.id,
                    expected_version=1,
                ),
                "emulator-note-delete",
            )

        results = self.concurrent(
            delete,
            lambda: self.notes.update(
                self.identity(),
                self.tenant,
                note.id,
                NotePatch(body="Changed"),
                "emulator-note-edit",
                '"1"',
            ),
        )
        self.assertEqual(sum(isinstance(r, (Operation, NoteView)) for r in results), 1)
        if isinstance(next(r for r in results if isinstance(r, (Operation, NoteView))), NoteView):
            operation = self.memories.forget(
                self.identity(),
                self.tenant,
                DeleteNote(
                    kind="delete_resource",
                    target_type="note",
                    target_id=note.id,
                    expected_version=2,
                ),
                "emulator-note-delete-v2",
            )
        else:
            operation = next(r for r in results if isinstance(r, Operation))
        self.assertFalse(
            self.notes.list(self.identity(), self.tenant, query="SYNTHETIC_NOTE").items
        )
        self.fails(
            lambda: self.notes.get(self.identity(), self.tenant, note.id),
            ErrorCode.RESOURCE_NOT_FOUND,
        )
        assert self.memories.deletion_ledger is not None
        jobs = PurgeJobs(self.memories.jobs, self.memories.deletion_ledger)
        pages = FirestorePurgePages(self.db)
        handler = PurgeResource(
            jobs,
            pages,
            hooks={"cache": NoExternalCopies(), "export": NoExternalCopies()},
            page_size=2,
        )
        queue = LocalQueue()
        dispatcher = PurgeDispatcher(handler, self.store, queue)
        worker = Worker(jobs, LocalCaller(), {KIND: handler})
        dispatcher.recover(self.tenant)
        queue.drain(worker)
        self.assertFalse(self.db.document(note_path(self.tenant, note.id)).get().exists)
        saved = (
            self.db.document(operation_path(self.tenant, operation.operation_id)).get().to_dict()
        )
        assert saved is not None
        self.assertEqual(saved["operation"]["status"], "succeeded")
        job = jobs.get(self.tenant, job_id(self.tenant, KIND, operation.operation_id))
        assert job is not None
        self.assertEqual(job.status, "succeeded")

    def test_lost_create_commit_response_recovers_same_document(self) -> None:
        commit = self.db._firestore_api.commit

        def lost(*args: Any, **kwargs: Any) -> Any:
            commit(*args, **kwargs)
            raise ServiceUnavailable("synthetic loss")  # type: ignore[no-untyped-call]

        with patch.object(self.db._firestore_api, "commit", side_effect=lost):
            self.fails(self.create_note, ErrorCode.TEMPORARILY_UNAVAILABLE)
        note = self.create_note()
        found = [
            d
            for d in self.db.collection(f"tenants/{self.tenant}/notes").stream()
            if (d.to_dict() or {}).get("title") == self.note_body.title
        ]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].id, note.id)


class NotesRunIntegrationTests(TurnRunIntegrationFixture):
    def test_list_then_update_retires_inputs_in_the_real_mutation_transaction(self) -> None:
        memories = Memories(
            self.store,
            self.tenants,
            Cursors(b"n" * 32, now=lambda: self.now),
            ledger=self.ledger,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        notes = NoteRequests(memories)
        body = NoteCreate(
            title="Emulator source",
            body="OLD_NOTE_SOURCE",
            visibility=Visibility.SHARED,
            display_policy=DisplayPolicy.SHARED_DISPLAY,
        )
        note = notes.create(self.identity(), self.tenant, body, "emulator-update-source")
        other = notes.create(
            self.identity(),
            self.tenant,
            body.model_copy(update={"title": "Other"}),
            "emulator-other-source",
        )
        self.db.document(f"tenants/{self.tenant}/agents/{self.agent}").update(
            {"allowed_tools": ["notes.list", "notes.update"]}
        )
        changes = {"body": "NEW_NOTE_SOURCE"}
        model = MultiStageModel(
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
        self.handler = RunTurn(self.jobs, model, credential_epoch=self.tokens.epoch)
        self.worker = Worker(self.jobs, LocalCaller(), {"run_turn": self.handler})
        conv = self.conversation()
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            conv,
            TurnCreate(
                text=f"Update note {note.id}: " + json.dumps(changes, sort_keys=True),
                output_mode="text",
            ),
            "emulator-list-update",
        ).run_id
        self.execute(run)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, run).status, "completed"
        )
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(notes.get(self.identity(), self.tenant, note.id).version, 2)
        saved = self.db.document(f"tenants/{self.tenant}/runs/{run}").get().to_dict()
        assert saved is not None
        self.assertEqual(saved["tool_bindings"], [])
        self.assertEqual(saved["memory_records"], [])
        self.assertEqual(saved["continuation"], [])
        self.assertEqual(
            {n["note_id"] for n in saved["purge_dependencies"]["notes"]}, {note.id, other.id}
        )
        final = (
            self.db.document(
                f"tenants/{self.tenant}/conversations/{conv}/messages/{saved['final_message_id']}"
            )
            .get()
            .to_dict()
        )
        assert final is not None
        self.assertEqual(final["text"], NotesRuntime.UPDATE_ACK)
        self.assertEqual(
            len(list(self.db.collection(f"tenants/{self.tenant}/note_effects").stream())), 1
        )

"""Real memory producers and in-flight source changes, shared fake/Emulator cases."""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal, cast
from unittest.mock import patch
from uuid import uuid4

from brainoid_agent_core.types import ToolCall
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.entities import Entities, EntityCreate
from brainoid_api.repositories.memories import Memories, MemoryCreate
from brainoid_api.repositories.notes import NoteRequests
from brainoid_api.repositories.reminders import ReminderRequests
from brainoid_api.repositories.tasks import TaskRequests
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_contracts.errors import APIError
from brainoid_contracts.guards import GuardedTransaction
from brainoid_contracts.transactions import Document
from brainoid_memory.calendar_proposal_access import check_origin
from brainoid_memory.calendar_proposals import proposal_path
from brainoid_memory.calendar_reader import CalendarReader
from brainoid_memory.dto import MemoryRevision
from brainoid_memory.export_projection import project
from brainoid_memory.models import SourceRef
from brainoid_memory.notes import NoteCreate, NotePatch
from brainoid_memory.reminders import ReminderCreate, ReminderPatch
from brainoid_memory.sources import CanonicalSources
from brainoid_memory.tasks import TaskCreate, TaskPatch
from brainoid_policy.scopes import PolicyDenied
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.models import ConversationCreate, TurnCreate
from test_memory_revalidation import MatchedClock

from fakes.calendar_events import EventsProvider
from fakes.run_confirmation import MultiStageModel


def _set_model(fixture: Any, model: MultiStageModel) -> None:
    fixture.model = model


class ProposalSourceScenarios:
    def setup_sources(self: Any, kind: Literal["person", "room", "appliance"] = "person") -> None:
        self.source_now = self.jobs.now
        self.handler.clock = MatchedClock(self.source_now)
        self.source_provider = cast(EventsProvider, self.proposals.events.reader.provider)
        options = dict(
            ledger=self.ledger,
            credentials=self.repo,
            now=self.source_now,
            allow_untracked_bootstrap=True,
        )
        cursors = Cursors(b"s" * 32, now=self.source_now)
        self.source_entities = Entities(self.store, self.tenants, cursors, **options)
        self.source_memories = Memories(self.store, self.tenants, cursors, **options)
        self.source_notes = NoteRequests(self.source_memories)
        self.person = self.source_entities.create(
            self.identity(),
            self.tenant,
            EntityCreate(
                kind=kind,
                label="Alex",
                visibility=Visibility.SHARED,
                display_policy=DisplayPolicy.SHARED_DISPLAY,
            ),
            str(uuid4()),
        ).id

    def source_fact(
        self: Any,
        *,
        expires: bool = False,
        note: bool = False,
        kind: Literal["person", "room", "appliance"] = "person",
    ) -> Any:
        self.setup_sources(kind)
        refs: tuple[SourceRef, ...] = ()
        if note:
            self.source_note = self.source_notes.create(
                self.identity(),
                self.tenant,
                NoteCreate(
                    title="source note",
                    body="SOURCE_NOTE",
                    visibility=Visibility.SHARED,
                    display_policy=DisplayPolicy.SHARED_DISPLAY,
                ),
                str(uuid4()),
            )
            refs = (SourceRef(kind="note", resource_id=self.source_note.id, version=1),)
        body = MemoryCreate(
            subject_entity_id=self.person,
            predicate="prefers",
            value="SYNTHETIC_SOURCE",
            visibility=Visibility.SHARED,
            display_policy=DisplayPolicy.SHARED_DISPLAY,
            source_refs=refs,
            valid_to=self.source_now() + timedelta(seconds=1) if expires else None,
        )
        return self.source_memories.create(self.identity(), self.tenant, body, str(uuid4()))

    def source_run(self: Any) -> ResourceId:
        parent = self.conversation(private=True)
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            parent,
            TurnCreate(text="Alex", output_mode="text"),
            str(uuid4()),
        ).run_id
        self.execute(run)
        return ResourceId(run)

    def source_records(self: Any) -> list[tuple[str, Document]]:
        return cast(
            list[tuple[str, Document]],
            self.store.run(lambda tx: tx.collection(f"tenants/{self.tenant}/calendar_proposals")),
        )

    def source_advance(self: Any) -> None:
        if hasattr(self, "clock"):
            self.clock.advance(1)
        else:
            self.now += timedelta(seconds=1)

    def assert_no_new_proposal(self: Any, before: list[tuple[str, Document]]) -> None:
        self.assertEqual(self.source_records(), before)
        receipts = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/idempotency_records")
        )
        executions = self.store.run(
            lambda tx: tx.collection(f"tenants/{self.tenant}/tool_executions")
        )
        effects = {row["effect_id"] for _, row in executions if row["name"] == "calendar.propose"}
        self.assertFalse(any(row["result"].get("resource_id") in effects for _, row in receipts))

    def during_provider(self: Any, mutate: Callable[[], None]) -> None:
        def after() -> None:
            self.source_provider.after = lambda: None
            mutate()

        self.source_provider.after = after

    def test_actual_fact_revision_during_provider_cannot_save_proposal(self: Any) -> None:
        fact = self.source_fact()
        before = self.source_records()
        self.during_provider(
            lambda: self.source_memories.revise(
                self.identity(),
                self.tenant,
                fact.id,
                MemoryRevision(
                    replacement=MemoryCreate(
                        subject_entity_id=self.person,
                        predicate="prefers",
                        value="CORRECTED_SOURCE",
                        visibility=Visibility.SHARED,
                        display_policy=DisplayPolicy.SHARED_DISPLAY,
                    )
                ),
                str(uuid4()),
                '"1"',
            )
        )
        self.source_run()
        saved = self.store.run(lambda tx: tx.get(f"tenants/{self.tenant}/facts/{fact.id}"))
        self.assertEqual(saved["status"], "superseded")
        self.assertEqual(saved["version"], 2)
        self.assert_no_new_proposal(before)

    def test_validity_boundary_during_provider_cannot_save_proposal(self: Any) -> None:
        self.source_fact(expires=True)
        before = self.source_records()
        self.during_provider(self.source_advance)
        self.source_run()
        self.assert_no_new_proposal(before)

    def test_source_only_revision_during_provider_cannot_save_proposal(self: Any) -> None:
        fact = self.source_fact(note=True)
        before = self.source_records()
        self.during_provider(
            lambda: self.source_notes.update(
                self.identity(),
                self.tenant,
                self.source_note.id,
                NotePatch(body="REVISED_NOTE"),
                str(uuid4()),
                '"1"',
            )
        )
        self.source_run()
        saved = self.store.run(lambda tx: tx.get(f"tenants/{self.tenant}/facts/{fact.id}"))
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["status"], "confirmed")
        self.assert_no_new_proposal(before)

    def test_entity_suppression_during_provider_cannot_save_proposal(self: Any) -> None:
        self.source_fact()
        before = self.source_records()

        def suppress() -> None:
            path = f"tenants/{self.tenant}/entities/{self.person}"

            def write(tx: Any) -> None:
                tx.set(path, {**tx.get(path), "suppress_reuse": True})

            self.store.run(write)

        self.during_provider(suppress)
        self.source_run()
        self.assert_no_new_proposal(before)

    def test_fact_guard_survives_last_read_until_canonical_write(self: Any) -> None:
        self.source_fact(expires=True)
        before = self.source_records()
        create = GuardedTransaction.create
        advanced: list[str] = []

        def delayed(tx: GuardedTransaction, path: str, doc: Document) -> None:
            if "/calendar_proposals/" in path and not advanced:
                advanced.append(path)
                self.source_advance()
            create(tx, path, doc)

        with patch.object(GuardedTransaction, "create", delayed):
            self.source_run()
        self.assertTrue(advanced)
        self.assert_no_new_proposal(before)

    def test_current_source_refs_fence_saved_preview_but_valid_to_is_not_ui_expiry(
        self: Any,
    ) -> None:
        self.source_fact(expires=True, note=True)
        run = self.source_run()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        assert proposal is not None
        self.source_advance()
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, proposal).status, "pending"
        )
        self.source_notes.update(
            self.identity(),
            self.tenant,
            self.source_note.id,
            NotePatch(body="REVISED_NOTE"),
            str(uuid4()),
            '"1"',
        )
        with self.assertRaises(APIError):
            self.proposals.get(self.identity(), self.tenant, proposal)
        with self.assertRaises(APIError):
            self.conversations.get_run(self.identity(), self.tenant, run)

    def test_source_change_before_provider_sends_no_request(self: Any) -> None:
        fact = self.source_fact()
        before = self.source_records()
        token = CalendarReader.token
        calls = len(self.source_provider.calls)
        armed: list[bool] = []

        def changed(reader: CalendarReader, record: Document) -> Any:
            result = token(reader, record)
            if not armed:
                armed.append(True)
                path = f"tenants/{self.tenant}/facts/{fact.id}"

                def write(tx: Any) -> None:
                    tx.set(path, {**tx.get(path), "suppress_reuse": True})

                self.store.run(write)
            return result

        with patch.object(CalendarReader, "token", changed):
            self.source_run()
        self.assertTrue(armed)
        self.assertEqual(len(self.source_provider.calls), calls)
        self.assert_no_new_proposal(before)

    def test_target_response_revalidates_sources(self: Any) -> None:
        self.source_fact(expires=True)
        self.arguments["change"].update(
            action="update", event_id="opaque-event", provider_etag='"provider-v3"'
        )
        _set_model(
            self,
            MultiStageModel(
                (
                    ToolCall(
                        call_key="proposal",
                        name="calendar.propose",
                        arguments_json=json.dumps(self.arguments),
                    ),
                )
            ),
        )
        self.restart()
        self.handler.clock = MatchedClock(self.source_now)
        before = self.source_records()
        event = self.source_provider.event

        def changed(*args: Any) -> Any:
            result = event(*args)
            self.source_advance()
            return result

        with patch.object(self.source_provider, "event", changed):
            self.source_run()
        self.assert_no_new_proposal(before)

    def direct_result_change(self: Any, kind: str) -> None:
        self.setup_sources()
        scope = dict(visibility="shared", display_policy="shared_display")
        service: Any
        body: Any
        if kind == "notes":
            service = self.source_notes
            body = NoteCreate.model_validate(dict(title="source note", body="SOURCE_NOTE", **scope))
            changes: Any = NotePatch(body="changed")
        elif kind == "tasks":
            service = TaskRequests(self.source_memories)
            body = TaskCreate.model_validate(dict(title="source task", **scope))
            changes = TaskPatch(title="changed")
        else:
            service = ReminderRequests(self.source_memories)
            body = ReminderCreate.model_validate(
                dict(
                    title="source reminder",
                    due_at=self.source_now() + timedelta(hours=1),
                    timezone="UTC",
                    delivery_target=dict(kind="user", id=self.identity().user_id),
                    **scope,
                )
            )
            changes = ReminderPatch(title="changed")
        direct = service.create(self.identity(), self.tenant, body, str(uuid4()))
        agent = getattr(self, "agent_id", None) or self.agent

        def allow(tx: Any) -> None:
            path = f"tenants/{self.tenant}/agents/{agent}"
            tx.set(path, {**tx.get(path), "allowed_tools": [kind + ".list", "calendar.propose"]})

        self.store.run(allow)
        _set_model(
            self,
            MultiStageModel(
                (
                    ToolCall(call_key="source", name=kind + ".list", arguments_json="{}"),
                    ToolCall(
                        call_key="proposal",
                        name="calendar.propose",
                        arguments_json=json.dumps(self.arguments),
                    ),
                )
            ),
        )
        self.restart()
        self.handler.clock = MatchedClock(self.source_now)
        before = self.source_records()
        self.during_provider(
            lambda: service.update(
                self.identity(), self.tenant, direct.id, changes, str(uuid4()), '"1"'
            )
        )
        run = self.source_run()
        saved = self.store.run(lambda tx: tx.get(f"tenants/{self.tenant}/runs/{run}"))
        self.assertTrue(saved["tool_bindings"])
        self.assertTrue(self.source_provider.calls)
        self.assert_no_new_proposal(before)

    def test_consumed_note_tool_rechecked_before_proposal_write(self: Any) -> None:
        self.direct_result_change("notes")

    def test_consumed_task_tool_rechecked_before_proposal_write(self: Any) -> None:
        self.direct_result_change("tasks")

    def test_consumed_reminder_tool_rechecked_before_proposal_write(self: Any) -> None:
        self.direct_result_change("reminders")

    def test_history_only_fact_rechecked_during_provider(self: Any) -> None:
        fact = self.source_fact()
        _set_model(self, MultiStageModel(()))
        self.restart()
        self.handler.clock = MatchedClock(self.source_now)
        parent = self.conversation(private=True)

        def turn(text: str) -> ResourceId:
            result = self.conversations.turn(
                self.identity(),
                self.tenant,
                parent,
                TurnCreate(text=text, output_mode="text"),
                str(uuid4()),
            ).run_id
            return ResourceId(result)

        first = turn("Alex")
        self.execute(first)
        self.assertEqual(
            self.conversations.get_run(self.identity(), self.tenant, first).status, "completed"
        )
        _set_model(
            self,
            MultiStageModel(
                (
                    ToolCall(
                        call_key="proposal",
                        name="calendar.propose",
                        arguments_json=json.dumps(self.arguments),
                    ),
                )
            ),
        )
        self.restart()
        self.handler.clock = MatchedClock(self.source_now)
        before = self.source_records()
        path = f"tenants/{self.tenant}/facts/{fact.id}"

        def suppress() -> None:
            def write(tx: Any) -> None:
                tx.set(path, {**tx.get(path), "suppress_reuse": True})

            self.store.run(write)

        self.during_provider(suppress)
        second = turn("zzz_no_match")
        self.execute(second)
        run = self.store.run(lambda tx: tx.get(f"tenants/{self.tenant}/runs/{second}"))
        self.assertFalse(run["memory_records"])
        self.assertIn(first, run["history_run_ids"])
        self.assertTrue(self.source_provider.calls)
        self.assert_no_new_proposal(before)

    def entity_output_fence(self: Any, kind: Literal["room", "appliance"]) -> None:
        self.source_fact(kind=kind)
        if hasattr(self, "credentials"):
            credentials = self.credentials()
        else:
            pairing = self.pairing()
            self.confirm(pairing)
            credentials = self.exchange(pairing)
        device = credentials.device_id
        path = f"tenants/{self.tenant}/devices/{device}"
        field = "allowed_room_ids" if kind == "room" else "allowed_entity_ids"
        scopes = ["shared", "conversation", "memory.read", "calendar.propose"]

        def configure(tx: Any) -> None:
            tx.set(
                path,
                {
                    **tx.get(path),
                    "capabilities": ["display"],
                    "allowed_scopes": scopes,
                    field: [self.person],
                },
            )

        self.store.run(configure)
        self.sessions.start(
            self.identity(),
            self.tenant,
            device,
            PrivateSessionCreate(
                acknowledge_shared_space=True,
                allowed_scopes=["conversation", "memory.read", "calendar.propose"],
            ),
            str(uuid4()),
            "calendar-output",
        )
        parent = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=getattr(self, "agent_id", None) or self.agent,
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
            ),
            str(uuid4()),
        ).id
        run = self.conversations.turn(
            self.identity(),
            self.tenant,
            parent,
            TurnCreate(text="Alex", output_mode="text", output_device_id=device),
            str(uuid4()),
        ).run_id
        self.execute(run)
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        assert proposal is not None
        canonical = proposal_path(self.tenant, proposal)

        def reuse(tx: Any) -> None:
            check_origin(
                tx,
                self.proposals.policy(tx),
                tx.get(canonical),
                now=self.source_now,
                credential_epoch=self.tokens.epoch,
                sources=CanonicalSources(SavedMessages()),
                for_model=True,
            )

        def export(tx: Any) -> Any:
            access = self.source_memories._open(tx, self.identity(), self.tenant, None)
            return project(tx, access, canonical, scope="own")

        self.store.run(reuse)
        self.assertIsNotNone(self.store.run(export))
        self.assertEqual(
            self.proposals.get(self.identity(), self.tenant, proposal).status, "pending"
        )

        # Isolate the entity allowlist restriction from the separate version/session fence.
        def restrict(tx: Any) -> None:
            tx.set(path, {**tx.get(path), field: []})

        self.store.run(restrict)
        with self.assertRaises(PolicyDenied):
            self.store.run(reuse)
        self.assertIsNone(self.store.run(export))
        with self.assertRaises(APIError):
            self.proposals.get(self.identity(), self.tenant, proposal)

    def test_room_output_allowlist_preserved_in_model_preview_and_export(self: Any) -> None:
        self.entity_output_fence("room")

    def test_appliance_output_allowlist_preserved_in_model_preview_and_export(self: Any) -> None:
        self.entity_output_fence("appliance")

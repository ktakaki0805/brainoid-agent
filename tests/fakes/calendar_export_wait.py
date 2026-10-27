"""CAL05 retained-derived export wait, actual admission/worker/purge and local ZIPs."""

from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.services.calendar_disconnect import CalendarDisconnect
from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Visibility
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.errors import APIError
from brainoid_jobs import AuthorizationUnavailable
from brainoid_memory import CanonicalSources, EntityInput, FactInput, MemoryRepository
from brainoid_memory.calendar_export import barrier_path
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.calendar_reader import integration_path, integration_stamp
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.forget import operation_path
from brainoid_memory.notes import NoteCreate, Notes, note_path
from brainoid_policy.control_ledger import LedgerError
from brainoid_policy.models import UserSubject
from brainoid_policy.scopes import PolicyUnavailable

from fakes.memory_repository import AuthenticatedCaller, context


class CalendarExportWaitScenarios:
    def retained_sources(self: Any) -> tuple[list[str], list[str]]:
        caller = AuthenticatedCaller(UserSubject(user_id=self.uid))
        caller.valid_from = self.clock.now()
        caller.valid_to = self.clock.now() + timedelta(hours=1)
        repo = MemoryRepository(
            self.store,
            authorizer=caller,
            sources=CanonicalSources(),
            ledger=self.ledger,
            now=self.clock.now,
            allow_untracked_bootstrap=True,
        )
        scope = context(self.tenant, self.uid)
        entity = repo.create_entity(
            scope,
            ResourceId(str(uuid4())),
            EntityInput(
                kind="person",
                label="Synthetic",
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.NEVER,
            ),
        )
        derived, ordinary = [], []
        for selected in (False, True):
            marker = "WAIT_SELECTED" if selected else "WAIT_ORDINARY"
            note = Notes(self.store, now=self.clock.now).mutate(
                lambda tx: repo.facts(tx, scope),
                NoteCreate(
                    title=marker,
                    body=marker + "_NOTE",
                    visibility=Visibility.PRIVATE,
                    display_policy=DisplayPolicy.NEVER,
                ),
                "calendar-wait-note-" + marker,
            )
            paths = [note_path(self.tenant, note.id)]
            for origin in ("user_explicit", "imported", "inferred"):
                fact = repo.create_fact(
                    scope,
                    ResourceId(str(uuid4())),
                    FactInput.model_validate(
                        {
                            "subject_entity_id": entity.id,
                            "predicate": "prefers",
                            "value": marker + "_FACT_" + origin,
                            "visibility": "private",
                            "display_policy": "never",
                            "valid_from": self.clock.now(),
                            "origin": origin,
                            "status": "candidate" if origin == "inferred" else "confirmed",
                            "source_refs": [{"kind": "note", "resource_id": note.id, "version": 1}],
                        }
                    ),
                )
                paths.append(f"tenants/{self.tenant}/facts/{fact.id}")
            if selected:
                integration = self.get(integration_path(self.tenant, self.integration))
                # Explicit retained historical lineage fixture, not a public Note producer.
                self.put(
                    paths[0],
                    {
                        **self.get(paths[0]),
                        "calendar_sources": [
                            {
                                "integration_id": self.integration,
                                "calendar_id": self.query.calendar_id,
                                "stamp": integration_stamp(integration),
                            }
                        ],
                    },
                )
                derived.extend(paths)
            else:
                ordinary.extend(paths)
        return derived, ordinary

    def assert_finished_archive(
        self: Any, operation: ResourceId, derived: list[str], ordinary: list[str]
    ) -> None:
        # Physical read precedes download, whose cleanup must not hide leftovers.
        content = self.archive_bytes(operation)
        self.assertNotIn("WAIT_SELECTED", content)
        self.assertIn("WAIT_ORDINARY_NOTE", content)
        self.assertIn("WAIT_ORDINARY_FACT_imported", content)
        for path in derived:
            self.assertIsNone(self.store.run(lambda tx, path=path: tx.get(path)))
        for path in ordinary:
            self.assertIsNotNone(self.store.run(lambda tx, path=path: tx.get(path)))
        self.assertTrue(self.export_requests.download(self.identity(), self.tenant, operation))

    def test_disconnect_new_archive_waits_all_projections_and_secret_receipt_without_attempts(
        self: Any,
    ) -> None:
        self.setup_archives()
        derived, ordinary = self.retained_sources()
        old = self.admit_archive("calendar-wait-original-archive")
        self.generate_archive(old)
        self.assertIn("WAIT_SELECTED", self.archive_bytes(old))
        secret = self.get(integration_path(self.tenant, self.integration))["secret_ref"]
        self.disconnect()
        intent = self.stop_intent()
        new = self.admit_archive("calendar-wait-new-archive")
        with (
            patch("brainoid_worker.export_data.ensure_ready", side_effect=PolicyUnavailable()),
            self.assertRaises(AuthorizationUnavailable),
        ):
            self.generate_archive(new)
        self.clock.advance(61)
        # Whole generation is guarded before any category calls project, including
        # facts, notes, tasks, reminders, notifications, messages, integrations.
        for _ in range(4):
            with patch("brainoid_worker.export_data.project") as project:
                self.generate_archive(new, waiting=True)
                project.assert_not_called()
            self.clock.advance(61)
        self.assertEqual(new, self.admit_archive("calendar-wait-new-archive"))
        self.prepare_purge()
        with patch.object(self.secrets, "invalidate", side_effect=SecretUnavailable()):
            self.drain_disconnect()
        self.assertEqual(
            self.get(barrier_path(self.tenant, subject_id(self.uid)))["pending"],
            {intent.operation_id: 1},
        )
        self.generate_archive(new, waiting=True)
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "pending",
        )
        self.clock.advance(61)
        self.drain_disconnect()
        self.assertEqual(
            self.get(barrier_path(self.tenant, subject_id(self.uid))),
            {"epoch": 1, "completed": 1, "pending": {}},
        )
        self.assert_archive_erased(old)
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(secret)
        self.generate_archive(new)
        self.assert_finished_archive(new, derived, ordinary)

    def test_disconnect_reservation_guard_survives_ledger_failure_missing_operation_and_producers(
        self: Any,
    ) -> None:
        self.setup_archives()
        derived, ordinary = self.retained_sources()
        producer = self.get(integration_path(self.tenant, self.integration))
        with (
            patch.object(self.deletion_ledger, "append", side_effect=LedgerError()),
            self.assertRaises(APIError),
        ):
            self.disconnect()
        stopped = self.get(integration_path(self.tenant, self.integration))
        operation = ResourceId(stopped["disconnect_operation_id"])
        reserved = self.get(f"tenants/{self.tenant}/calendar_disconnect_requests/{operation}")
        intent = DeletionIntent.model_validate(reserved["intent"])
        self.assertIsNone(self.deletion_ledger.get(operation))
        self.assertIsNone(self.store.run(lambda tx: tx.get(operation_path(self.tenant, operation))))
        new = self.admit_archive("calendar-wait-ledger-failure")
        self.generate_archive(new, waiting=True)
        # Unrelated owner's generation still works during the wait.
        other = self.admit_archive("calendar-wait-other-owner", uid=FirebaseUid("demo-a-owner"))
        self.generate_archive(other)
        self.assertTrue(self.archive_bytes(other))
        for path in (
            integration_path(self.tenant, self.integration),
            f"tenants/{self.tenant}/calendar_authorizations/{producer['authorization_id']}",
        ):
            self.store.run(lambda tx, path=path: tx.delete(path))
        self.restart_archives()
        restarted = CalendarDisconnect(self.service, self.deletion_ledger)
        restarted.recover(self.tenant, operation)
        # A lost public operation can be reconstructed only for the exact durable
        # reservation/fence/ledger and bound existing waiting job.
        self.store.run(lambda tx: tx.delete(operation_path(self.tenant, operation)))
        self.clock.advance(61)
        self.generate_archive(new, waiting=True)
        restarted.recover(self.tenant, operation)
        self.prepare_purge()
        self.drain_disconnect(intent.operation_id)
        self.assertEqual(
            self.get(operation_path(self.tenant, operation))["operation"]["status"], "succeeded"
        )
        self.clock.advance(61)
        self.generate_archive(new)
        self.assert_finished_archive(new, derived, ordinary)
        self.assertTrue(
            self.export_requests.download(
                self.identity(FirebaseUid("demo-a-owner")), self.tenant, other
            )
        )

    def test_disconnect_during_scan_and_delayed_put_never_leaves_old_archive(self: Any) -> None:
        self.setup_archives()
        self.retained_sources()
        old = self.admit_archive("calendar-wait-scan-race")
        children = self.purge_pages.children
        stopped: list[bool] = []

        def scan(parent: str, **kwargs: Any) -> Any:
            result = children(parent, **kwargs)
            if parent == f"tenants/{self.tenant}/notes" and not stopped:
                stopped.append(True)
                self.disconnect()
            return result

        with patch.object(self.purge_pages, "children", side_effect=scan):
            self.generate_archive(old)
        self.assertEqual(stopped, [True])
        self.assert_archive_erased(old)
        self.prepare_purge()
        self.drain_disconnect()
        # A fresh connection admits a fresh export. Stop its exact integration
        # after staging but before physical put; the old slot tombstone wins.
        state, authorization = self.start("calendar-wait-new-put-race")
        self.service.callback(state, "synthetic-code", None)
        integration = ResourceId(
            self.get(f"tenants/{self.tenant}/calendar_authorizations/{authorization}")[
                "integration_id"
            ]
        )
        late = self.admit_archive("calendar-wait-staged-late-put")
        put = self.export_storage.put

        def delayed(identity: ResourceId, content: bytes, artifact: Any) -> None:
            self.disconnects.disconnect(
                self.identity(), self.tenant, integration, "calendar-wait-put-disconnect", '"1"'
            )
            intent = self.stop_intent(integration)
            self.drain_disconnect(intent.operation_id)
            put(identity, content, artifact)

        with patch.object(self.export_storage, "put", side_effect=delayed):
            self.generate_archive(late)
        self.assert_archive_erased(late)
        safe = self.admit_archive("calendar-wait-after-all-cleanup")
        self.generate_archive(safe)
        self.assertNotIn("WAIT_SELECTED", self.archive_bytes(safe))
        self.assertTrue(self.export_requests.download(self.identity(), self.tenant, safe))

"""CAL05 review regressions shared by strict fake and Firestore transactions.

Facts/notes use their actual repositories. Descendant history/vector copies and
retained Calendar lineage are explicit synthetic storage fixtures, not claims
that today's direct-user note producer emits Calendar-derived notes.
"""

import io
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import uuid4

from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.services.calendar_disconnect import CalendarDisconnect
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts import DisplayPolicy, FirebaseUid, ResourceId, Visibility
from brainoid_contracts.deletion import subject_id
from brainoid_jobs import Jobs
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources, EntityInput, FactInput, MemoryRepository
from brainoid_memory.calendar_export import CalendarCleanupPending, barrier_path
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.calendar_reader import integration_path
from brainoid_memory.export_manifest import KIND as EXPORT_KIND
from brainoid_memory.export_manifest import ExportCreate
from brainoid_memory.exports import Exports, export_path, registry_path
from brainoid_memory.forget import operation_path
from brainoid_memory.lifecycle import KIND
from brainoid_memory.notes import NoteCreate, Notes, note_path
from brainoid_policy.models import UserSubject
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller

from fakes.memory_repository import AuthenticatedCaller, context


class DisconnectRegressions:
    def setup_archives(self: Any) -> None:
        self.export_offset = 0
        self.export_now = lambda: self.clock.now() + timedelta(seconds=self.export_offset)
        self.export_storage = LocalExportObjects(
            Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports",
            b"e" * 32,
            now=self.export_now,
        )
        self.restart_archives()

    def restart_archives(self: Any) -> None:
        self.calendar_exports = Exports(
            Jobs(self.store, self.ledger, now=self.export_now, allow_untracked_bootstrap=True),
            self.export_storage,
            self.purge_pages,
            CanonicalSources(SavedMessages()),
            now=self.export_now,
        )
        self.export_requests = ExportRequests(self.calendar_exports, self.tenants)

    def admit_archive(
        self: Any, key: str, *, tenant: ResourceId | None = None, uid: FirebaseUid | None = None
    ) -> ResourceId:
        identity = self.identity(uid).model_copy(
            update={"issued_at": self.clock.now() - timedelta(seconds=30)}
        )
        operation = self.export_requests.create(
            identity,
            tenant or self.tenant,
            ExportCreate(kind="export", scope="my_data"),
            key,
        ).operation_id
        return ResourceId(operation)

    def generate_archive(
        self: Any, operation: ResourceId, tenant: ResourceId | None = None, *, waiting: bool = False
    ) -> None:
        tenant = tenant or self.tenant
        delivery = self.calendar_exports.jobs.prepare_dispatch(
            tenant, job_id(tenant, EXPORT_KIND, operation)
        )
        assert delivery is not None
        worker = Worker(
            self.calendar_exports.jobs,
            LocalCaller(),
            {EXPORT_KIND: ExportData(self.calendar_exports)},
        )
        if waiting:
            with self.assertRaises(CalendarCleanupPending):
                worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))
            job = self.calendar_exports.jobs.get(tenant, job_id(tenant, EXPORT_KIND, operation))
            self.assertEqual(job.status, "pending")
            self.assertEqual(job.attempts, 0)
            self.assertEqual(self.get(registry_path(operation))["state"], "pending")
            for suffix in ("sealed", "revoked"):
                self.assertFalse((self.export_storage.directory / f"{operation}.{suffix}").exists())
        else:
            worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def archive_bytes(self: Any, operation: ResourceId) -> str:
        # Inspect physical storage before download/reaper can mask an erasure defect.
        content, _ = self.export_storage.get(operation)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return archive.read("data.json").decode()

    def assert_archive_erased(self: Any, operation: ResourceId) -> None:
        self.assertFalse((self.export_storage.directory / f"{operation}.sealed").exists())
        self.assertTrue((self.export_storage.directory / f"{operation}.revoked").exists())
        self.assertTrue(self.get(registry_path(operation))["erased"])

    def stop_intent(self: Any, integration: ResourceId | None = None) -> Any:
        record = self.get(integration_path(self.tenant, integration or self.integration))
        intent = self.deletion_ledger.get(ResourceId(record["disconnect_operation_id"]))
        assert intent is not None
        return intent

    def test_disconnect_formal_fact_origins_and_derived_history_vectors(self: Any) -> None:
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
                label="Synthetic person",
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.NEVER,
            ),
        )
        ordinary: dict[str, Any] = {}
        selected: list[str] = []
        for derived in (False, True):
            note = Notes(self.store, now=self.clock.now).mutate(
                lambda tx: repo.facts(tx, scope),
                NoteCreate(
                    title="Synthetic",
                    body="Synthetic source",
                    visibility=Visibility.PRIVATE,
                    display_policy=DisplayPolicy.NEVER,
                ),
                f"calendar-fact-note-{derived}",
            )
            npath = note_path(self.tenant, note.id)
            for origin in ("user_explicit", "imported", "inferred"):
                fact = repo.create_fact(
                    scope,
                    ResourceId(str(uuid4())),
                    FactInput.model_validate(
                        {
                            "subject_entity_id": entity.id,
                            "predicate": "prefers",
                            "value": "synthetic",
                            "visibility": "private",
                            "display_policy": "never",
                            "valid_from": self.clock.now(),
                            "origin": origin,
                            "status": "candidate" if origin == "inferred" else "confirmed",
                            "source_refs": [{"kind": "note", "resource_id": note.id, "version": 1}],
                        }
                    ),
                )
                fpath = f"tenants/{self.tenant}/facts/{fact.id}"
                self.assertEqual(self.get(fpath)["origin"], origin)
                copies = [fpath, f"{fpath}/history/{uuid4()}"]
                self.put(copies[1], fact.model_dump(mode="json"))
                for generation in ("a" * 32, "b" * 32):
                    vector = f"tenants/{self.tenant}/memory_vectors_{generation}/{fact.id}"
                    self.put(
                        vector,
                        {"tenant_id": self.tenant, "fact_id": fact.id, "text": "Synthetic copy"},
                    )
                    copies.append(vector)
                for p in copies:
                    if derived:
                        selected.append(p)
                    else:
                        ordinary[p] = self.get(p)
            if derived:
                # Retained provenance on a historical copy; facts remain formal DTOs
                # and are linked through real source_refs, not invented origin objects.
                self.put(
                    npath,
                    {**self.get(npath), "calendar_sources": [{"integration_id": self.integration}]},
                )
                selected.append(npath)
            else:
                ordinary[npath] = self.get(npath)
        secret = self.get(integration_path(self.tenant, self.integration))["secret_ref"]
        self.disconnect()
        self.prepare_purge()
        self.drain_disconnect()
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "complete",
        )
        with self.assertRaises(SecretUnavailable):
            self.secrets.get(secret)
        for p in selected:
            self.assertIsNone(self.store.run(lambda tx, p=p: tx.get(p)), p)
        for p, doc in ordinary.items():
            self.assertEqual(self.get(p), doc, p)

    def test_disconnect_archive_order_ignores_clocks_and_keeps_safe_new_archives(self: Any) -> None:
        self.setup_archives()
        old = []
        for offset in (-1, 0, 1):
            self.export_offset = offset
            operation = self.admit_archive(f"calendar-old-offset-{offset}")
            self.generate_archive(operation)
            self.assertIn(self.query.calendar_id, self.archive_bytes(operation))
            old.append(operation)
        # Legacy registry admitted before the first stop has an explicit default of 0.
        legacy = self.get(registry_path(old[0]))
        legacy.pop("calendar_epoch")
        self.put(registry_path(old[0]), legacy)
        self.disconnect()
        self.assertEqual(
            self.get(integration_path(self.tenant, self.integration))["allowed_calendar_ids"], []
        )
        new = []
        for offset in (-1, 0, 1):
            self.export_offset = offset
            operation = self.admit_archive(f"calendar-new-offset-{offset}")
            self.generate_archive(operation, waiting=True)
            new.append(operation)
        self.export_offset = 0
        self.prepare_purge()
        self.drain_disconnect()
        self.assertEqual(
            self.disconnects.get(self.identity(), self.tenant, self.integration).cleanup_status,
            "complete",
        )
        for operation in old:
            self.assert_archive_erased(operation)
        self.clock.advance(61)
        for operation in new:
            self.generate_archive(operation)
            self.assertNotIn(self.query.calendar_id, self.archive_bytes(operation))
            self.assertTrue(self.export_requests.download(self.identity(), self.tenant, operation))

    def test_disconnect_archive_isolation_restart_missing_producers_and_old_replay(
        self: Any,
    ) -> None:
        self.setup_archives()
        old = self.admit_archive("calendar-export-original")
        self.generate_archive(old)
        other_user = FirebaseUid("demo-a-owner")
        other = self.admit_archive("calendar-export-other-user", uid=other_user)
        self.generate_archive(other)
        other_tenant = next(
            ResourceId(p.split("/")[1])
            for p, _ in self.store.run(lambda tx: tx.collection("tenants"))
            if p != f"tenants/{self.tenant}"
        )
        member = self.store.run(
            lambda tx: tx.query(f"tenants/{self.tenant}/memberships", {"user_id": self.uid})
        )[0][1]
        self.put(
            f"tenants/{other_tenant}/memberships/{uuid4()}", {**member, "tenant_id": other_tenant}
        )
        elsewhere = self.admit_archive("calendar-export-other-tenant", tenant=other_tenant)
        self.generate_archive(elsewhere, other_tenant)
        saved = self.get(integration_path(self.tenant, self.integration))
        self.disconnect()
        intent = self.stop_intent()
        # Independent inventory remains sufficient with all canonical producer roots absent.
        for p in (
            integration_path(self.tenant, self.integration),
            f"tenants/{self.tenant}/calendar_authorizations/{saved['authorization_id']}",
            export_path(self.tenant, old),
            operation_path(self.tenant, old),
        ):
            self.store.run(lambda tx, p=p: tx.delete(p))
        self.restart_archives()
        new = self.admit_archive("calendar-export-new-after-restart")
        self.generate_archive(new, waiting=True)
        self.prepare_purge()
        self.drain_disconnect(intent.operation_id)
        self.clock.advance(61)
        self.generate_archive(new)
        self.calendar_exports.invalidate(intent, key=f"{intent.operation_id}:export:v1")
        self.calendar_exports.invalidate(intent, key=f"{intent.operation_id}:export:v1")
        self.assert_archive_erased(old)
        for operation, tenant, uid in (
            (new, self.tenant, self.uid),
            (other, self.tenant, other_user),
            (elsewhere, other_tenant, self.uid),
        ):
            self.assertTrue(self.archive_bytes(operation))
            self.assertTrue(self.export_requests.download(self.identity(uid), tenant, operation))

    def test_disconnect_multiple_integrations_barriers_and_restore_recovery(self: Any) -> None:
        self.setup_archives()
        self.disconnect()
        first = self.stop_intent()
        between = self.admit_archive("calendar-export-between-stops")
        self.generate_archive(between, waiting=True)
        another = next(ResourceId(r["id"]) for r in self.records() if r["id"] != self.integration)
        self.disconnects.disconnect(
            self.identity(), self.tenant, another, "calendar-disconnect-second", '"1"'
        )
        second = self.stop_intent(another)
        self.assertEqual([first.scopes[0].export_epoch, second.scopes[0].export_epoch], [1, 2])
        self.disconnect()  # Exact replay does not advance the admission barrier.
        self.assertEqual(self.get(barrier_path(self.tenant, subject_id(self.uid)))["epoch"], 2)
        state, _ = self.start("calendar-after-second-stop")
        self.service.callback(state, "synthetic-code", None)
        new = self.admit_archive("calendar-export-after-both-stops")
        self.generate_archive(new, waiting=True)
        self.calendar_exports.invalidate(second, key=f"{second.operation_id}:export:v1")
        self.calendar_exports.invalidate(first, key=f"{first.operation_id}:export:v1")
        self.assert_archive_erased(between)
        barrier = barrier_path(self.tenant, subject_id(self.uid))
        self.store.run(lambda tx: tx.delete(barrier))
        restarted = CalendarDisconnect(self.service, self.deletion_ledger)
        with self.assertRaisesRegex(ValueError, "restored"):
            restarted.recover(self.tenant, first.operation_id)
        self.prepare_purge()
        job = self.purge_jobs.get(self.tenant, job_id(self.tenant, KIND, second.operation_id))
        assert job is not None
        with self.assertRaisesRegex(ValueError, "restored"):
            self.store.run(lambda tx: self.purge_jobs.accepted(tx, job))
        # Restore the maximum accepted immutable epoch, never reset or infer wall time.
        # Nonzero epoch alone cannot prove which accepted purges are still pending.
        self.put(barrier, {"epoch": 2})
        with self.assertRaisesRegex(ValueError, "restored"):
            restarted.recover(self.tenant, first.operation_id)
        self.put(barrier, {"epoch": 2, "pending": {}})
        with self.assertRaisesRegex(ValueError, "restored"):
            restarted.recover(self.tenant, first.operation_id)
        self.put(
            barrier,
            {
                "epoch": 2,
                "completed": 0,
                "pending": {first.operation_id: 1, second.operation_id: 2},
            },
        )
        restarted.recover(self.tenant, first.operation_id)
        self.store.run(lambda tx: self.purge_jobs.accepted(tx, job))
        self.drain_disconnect(second.operation_id)
        self.assertEqual(self.get(barrier)["pending"], {first.operation_id: 1})
        restarted.recover(self.tenant, second.operation_id)
        self.assertEqual(self.get(barrier)["pending"], {first.operation_id: 1})
        self.clock.advance(61)
        self.generate_archive(new, waiting=True)
        self.drain_disconnect(first.operation_id)
        self.clock.advance(61)
        self.generate_archive(new)
        self.assertTrue(self.archive_bytes(new))
        self.assertTrue(self.export_requests.download(self.identity(), self.tenant, new))

    def test_disconnect_concurrent_archive_admission_and_inflight_old_generation(self: Any) -> None:
        self.setup_archives()
        old = self.admit_archive("calendar-inflight-before-stop")
        ready = Barrier(2)

        def export() -> ResourceId:
            ready.wait(10)
            return ResourceId(self.admit_archive("calendar-concurrent-export"))

        def stop() -> None:
            ready.wait(10)
            self.disconnect()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(export), pool.submit(stop)
            concurrent = a.result(timeout=30)
            b.result(timeout=30)
        intent = self.stop_intent()
        admitted = self.get(registry_path(concurrent))["calendar_epoch"]
        self.assertIn(admitted, (0, 1))
        self.generate_archive(old)
        self.generate_archive(concurrent, waiting=admitted == 1)
        self.prepare_purge()
        self.drain_disconnect()
        self.assert_archive_erased(old)
        if admitted < intent.scopes[0].export_epoch:
            self.assert_archive_erased(concurrent)
        else:
            self.clock.advance(61)
            self.generate_archive(concurrent)
            self.assertNotIn(self.query.calendar_id, self.archive_bytes(concurrent))
            self.assertTrue(self.export_requests.download(self.identity(), self.tenant, concurrent))

    def test_disconnect_concurrent_integrations_allocate_distinct_epochs(self: Any) -> None:
        self.setup_archives()
        other = next(ResourceId(r["id"]) for r in self.records() if r["id"] != self.integration)
        ready = Barrier(2)

        def stop(integration: ResourceId, version: int) -> None:
            ready.wait(10)
            self.disconnects.disconnect(
                self.identity(),
                self.tenant,
                integration,
                f"calendar-parallel-stop-{integration}",
                f'"{version}"',
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(stop, self.integration, 2), pool.submit(stop, other, 1)
            a.result(timeout=30)
            b.result(timeout=30)
        intents = [self.stop_intent(), self.stop_intent(other)]
        self.assertEqual(sorted(i.scopes[0].export_epoch for i in intents), [1, 2])
        new = self.admit_archive("calendar-after-parallel-stops")
        self.generate_archive(new, waiting=True)
        self.prepare_purge()
        first, second = sorted(intents, key=lambda i: i.scopes[0].export_epoch)
        self.drain_disconnect(first.operation_id)
        self.disconnects.recover(self.tenant, first.operation_id)
        self.assertEqual(
            self.get(barrier_path(self.tenant, subject_id(self.uid)))["pending"],
            {second.operation_id: 2},
        )
        self.clock.advance(61)
        self.generate_archive(new, waiting=True)
        self.drain_disconnect(second.operation_id)
        self.clock.advance(61)
        self.generate_archive(new)
        self.assertTrue(self.archive_bytes(new))

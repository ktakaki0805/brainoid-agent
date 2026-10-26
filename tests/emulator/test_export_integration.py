"""MEM-09 real Firestore: atomic admission, delayed publication and purge races."""

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from brainoid_api.adapters.local_authz_ledger import LocalAuthzObjects
from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.errors import APIError
from brainoid_api.services.deletion_requests import DeleteTenant, DeletionRequests
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts import ResourceId
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionLedger
from brainoid_memory.export_manifest import KIND, Artifact, ExportCreate
from brainoid_memory.export_projection import project
from brainoid_memory.exports import Exports, registry_path
from brainoid_memory.forget import ForgetCreate, Operation, operation_path
from brainoid_memory.lifecycle import KIND as LIFECYCLE_KIND
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.export_reaper import ExportReaper
from brainoid_worker.lifecycle_handlers import LifecycleHandlers
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import NoExternalCopies, PurgeResource
from google.api_core.exceptions import ServiceUnavailable
from test_memories_integration import MemoryIntegrationFixture


class ExportIntegrationTests(MemoryIntegrationFixture):
    def setUp(self) -> None:
        super().setUp()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        self.objects = LocalExportObjects(directory, b"e" * 32, now=lambda: self.now)
        self.pages = FirestorePurgePages(self.db)
        self.exports = Exports(
            self.memories.jobs,
            self.objects,
            self.pages,
            CanonicalSources(SavedMessages()),
            now=lambda: self.now,
        )
        self.requests = ExportRequests(self.exports, self.tenants)
        self.handler = ExportData(self.exports)
        self.worker = Worker(self.exports.jobs, LocalCaller(), {KIND: self.handler})
        self.memories.deletion_ledger = DeletionLedger(LocalAuthzObjects(self.directory))
        self.memory = self.create_memory()

    def create_export(self) -> Operation:
        return self.requests.create(
            self.identity(),
            self.tenant,
            ExportCreate(kind="export", scope="visible_tenant_data"),
            "export-real-firestore",
        )

    def generate(self, operation: Operation) -> None:
        delivery = self.exports.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, KIND, operation.operation_id)
        )
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def saved(self, path: str) -> Any:
        return self.db.document(path).get().to_dict()

    def test_concurrent_admission_and_current_private_projection(self) -> None:
        first, second = self.concurrent(self.create_export, self.create_export)
        self.assertIsInstance(first, Operation)
        self.assertEqual(first, second)
        assert isinstance(first, Operation)
        self.generate(first)
        content = self.requests.download(self.identity(), self.tenant, first.operation_id)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            body = json.loads(archive.read("data.json"))
        self.assertIn(self.memory.id, {entry["resource_id"] for entry in body["manifest"]})
        self.assertEqual(len(list(self.db.collection("export_artifacts").stream())), 1)

    def test_delayed_put_races_real_forget_and_purge_hook(self) -> None:
        operation = self.create_export()
        put = self.objects.put

        def delayed(key: ResourceId, content: bytes, artifact: Artifact) -> None:
            put(key, content, artifact)
            forgotten = self.memories.forget(
                self.identity(),
                self.tenant,
                ForgetCreate(kind="forget_memory", target_id=self.memory.id, expected_version=1),
                "forget-during-export-put",
            )
            assert self.memories.deletion_ledger is not None
            intent = self.memories.deletion_ledger.get(forgotten.operation_id)
            assert intent is not None
            self.exports.invalidate(intent, key=f"{intent.operation_id}:export:v1")

        with patch.object(self.objects, "put", side_effect=delayed):
            self.generate(operation)
        self.assertEqual(self.saved(registry_path(operation.operation_id))["state"], "revoked")
        self.assertFalse((self.objects.directory / (operation.operation_id + ".sealed")).exists())
        with self.assertRaises(APIError):
            self.requests.download(self.identity(), self.tenant, operation.operation_id)

    def test_manifest_edit_and_reaper_recovery_without_request(self) -> None:
        operation = self.create_export()
        self.generate(operation)
        self.revision(self.memory.id)
        ExportReaper(self.exports).step()
        self.assertTrue(self.saved(registry_path(operation.operation_id))["erased"])
        self.assertFalse((self.objects.directory / (operation.operation_id + ".sealed")).exists())

    def test_tenant_purge_deletes_metadata_before_hook_without_losing_object(self) -> None:
        operation = self.create_export()
        self.generate(operation)
        assert self.memories.deletion_ledger is not None
        requests = DeletionRequests(
            self.store, self.memories.deletion_ledger, self.ledger, now=lambda: self.now
        )
        tenant = self.saved(f"tenants/{self.tenant}")
        deleted = requests.create(
            self.identity(),
            DeleteTenant(kind="delete_tenant", confirmation_name=tenant["name"]),
            "delete-tenant-with-export",
            self.tenant,
        )
        jobs = PurgeJobs(self.exports.jobs, self.memories.deletion_ledger)
        purge = PurgeResource(
            jobs,
            self.pages,
            handlers=LifecycleHandlers(),
            hooks={"cache": NoExternalCopies(), "export": self.exports},
        )
        jid = job_id(self.tenant, LIFECYCLE_KIND, deleted.operation_id)
        jobs.activate(self.tenant, jid)
        delivery = jobs.prepare_dispatch(self.tenant, jid, queue_name=LIFECYCLE_KIND)
        assert delivery is not None
        Worker(jobs, LocalCaller(), {LIFECYCLE_KIND: purge}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        self.assertEqual(
            self.saved(operation_path(self.tenant, deleted.operation_id))["operation"]["status"],
            "succeeded",
        )
        self.assertFalse((self.objects.directory / (operation.operation_id + ".sealed")).exists())
        with self.assertRaises(APIError):
            self.requests.download(self.identity(), self.tenant, operation.operation_id)

    def test_admission_commit_reply_loss_replays_same_job_and_registry(self) -> None:
        original = self.db._firestore_api.commit
        lost = False

        def commit(**kwargs: Any) -> Any:
            nonlocal lost
            response = original(**kwargs)
            if not lost and any(
                "/export_artifacts/" in str(write)
                for write in kwargs.get("request", {}).get("writes", [])
            ):
                lost = True
                raise ServiceUnavailable("synthetic lost reply")  # type: ignore[no-untyped-call]
            return response

        with patch.object(self.db._firestore_api, "commit", side_effect=commit):
            with self.assertRaises(APIError):
                self.create_export()
        operation = self.create_export()
        self.assertTrue(lost)
        self.generate(operation)
        self.assertEqual(
            self.requests.get(self.identity(), self.tenant, operation.operation_id).status,
            "succeeded",
        )

    def test_real_source_ledger_outage_defers_then_completes_same_operation(self) -> None:
        from datetime import timedelta

        from brainoid_jobs import AuthorizationUnavailable
        from brainoid_policy.control_ledger import LedgerError
        from brainoid_worker import export_data

        operation = self.create_export()
        original = project
        failed = False

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            nonlocal failed
            if not failed:
                failed = True
                with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                    return original(*args, **kwargs)
            return original(*args, **kwargs)

        with (
            patch.object(export_data, "project", side_effect=unavailable),
            patch.object(self.objects, "put", wraps=self.objects.put) as put,
        ):
            with self.assertRaises(AuthorizationUnavailable):
                self.generate(operation)
            put.assert_not_called()
        self.assertTrue(failed)
        job = self.saved(
            f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, operation.operation_id)}"
        )
        self.assertEqual(
            (job["status"], job["attempts"], job["reason"]),
            ("pending", 0, "authorization_unavailable"),
        )
        self.assertEqual(job["checkpoints"], [])
        self.assertEqual(self.saved(registry_path(operation.operation_id))["state"], "pending")
        self.assertFalse((self.objects.directory / (operation.operation_id + ".revoked")).exists())
        self.now += timedelta(seconds=6)
        self.generate(operation)
        self.assertEqual(
            self.requests.get(self.identity(), self.tenant, operation.operation_id).status,
            "succeeded",
        )
        self.assertTrue(
            self.requests.download(self.identity(), self.tenant, operation.operation_id)
        )

    def test_real_commit_ledger_outage_reuses_completed_object_checkpoint(self) -> None:
        from datetime import timedelta

        from brainoid_jobs import AuthorizationUnavailable
        from brainoid_policy.control_ledger import LedgerError

        operation = self.create_export()
        commit, current = self.handler._commit, self.exports.current

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            with patch.object(self.ledger, "latest", side_effect=LedgerError()):
                return current(*args, **kwargs)

        def commit_outage(*args: Any, **kwargs: Any) -> None:
            with patch.object(self.exports, "current", side_effect=unavailable):
                commit(*args, **kwargs)

        with patch.object(self.objects, "put", wraps=self.objects.put) as put:
            with (
                patch.object(self.handler, "_commit", side_effect=commit_outage),
                self.assertRaises(AuthorizationUnavailable),
            ):
                self.generate(operation)
            job = self.saved(
                f"tenants/{self.tenant}/jobs/{job_id(self.tenant, KIND, operation.operation_id)}"
            )
            self.assertEqual((job["status"], job["attempts"]), ("pending", 0))
            self.assertEqual(job["checkpoints"][0]["state"], "completed")
            self.assertEqual(self.saved(registry_path(operation.operation_id))["state"], "staged")
            self.assertFalse(
                (self.objects.directory / (operation.operation_id + ".revoked")).exists()
            )
            self.now += timedelta(seconds=6)
            self.generate(operation)
            self.assertEqual(
                self.requests.get(self.identity(), self.tenant, operation.operation_id).status,
                "succeeded",
            )
            self.assertEqual(put.call_count, 1)

"""MEM-09 / T-001 / T-022: current authority, delayed I/O and physical erasure."""

import asyncio
import copy
import io
import json
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from brainoid_api.adapters.local_export_objects import LocalExportObjects
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError
from brainoid_api.routes.operation_download import ExportResponse
from brainoid_api.services.export_requests import ExportRequests
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import purge_barrier_path, subject_id
from brainoid_jobs.models import job_id
from brainoid_memory import CanonicalSources
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger, LifecycleScope
from brainoid_memory.export_manifest import KIND, Artifact, ExportCreate, ObjectRevoked
from brainoid_memory.exports import Exports, export_path, registry_path
from brainoid_memory.forget import ForgetCreate, operation_path
from brainoid_runs.memory_sources import SavedMessages
from brainoid_worker.entry import Worker
from brainoid_worker.export_data import ExportData
from brainoid_worker.export_reaper import ExportReaper
from brainoid_worker.local import LOCAL_AUTHORIZATION, LocalCaller
from fakes.authz_ledger import MemoryObjects
from fakes.memory_repository import CONVERSATION, NOW, OTHER, SOURCE, TENANT, USER
from fakes.purge import FakePurgePages
from test_deletion_requests import DeletionFixture
from test_memories import MemoryFixture
from test_turn_run import TurnRunFixture


class ExportFixture(MemoryFixture):
    def setUp(self) -> None:
        super().setUp()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "private-exports"
        self.storage = LocalExportObjects(directory, b"x" * 32, now=lambda: self.now)
        self.pages = FakePurgePages(self.store)
        self.exports = Exports(
            self.memories.jobs,
            self.storage,
            self.pages,
            CanonicalSources(SavedMessages()),
            now=lambda: self.now,
        )
        self.requests = ExportRequests(self.exports, self.tenants)
        self.handler = ExportData(self.exports)
        self.worker = Worker(self.exports.jobs, LocalCaller(), {KIND: self.handler})
        self.reaper = ExportReaper(self.exports)
        self.memories.deletion_ledger = DeletionLedger(MemoryObjects())
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.credentials,
            memories=self.memories,
            exports=self.requests,
        )
        self.operations_url = f"/v1/tenants/{TENANT}/operations"

    def create_export(
        self, key: str = "export-fixture-key-001", scope: str = "visible_tenant_data"
    ) -> ResourceId:
        response = self.client.post(
            self.operations_url,
            json={"kind": "export", "scope": scope},
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["kind"], "export")
        return ResourceId(response.json()["operation_id"])

    def generate(self, identity: ResourceId) -> None:
        delivery = self.exports.jobs.prepare_dispatch(TENANT, job_id(TENANT, KIND, identity))
        self.assertIsNotNone(delivery)
        assert delivery is not None
        self.worker.handle(LOCAL_AUTHORIZATION, delivery.model_dump(mode="json"))

    def ready(self) -> ResourceId:
        identity = self.create_export()
        self.generate(identity)
        saved = self.store.data[operation_path(TENANT, identity)]
        self.assertEqual(saved["operation"]["status"], "succeeded", saved)
        return identity

    def download(self, identity: ResourceId, tenant: ResourceId = TENANT) -> Any:
        return self.client.get(f"/v1/tenants/{tenant}/operations/{identity}/download")

    def archive(self, identity: ResourceId) -> dict[str, Any]:
        response = self.download(identity)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["data.json"])
            result: dict[str, Any] = json.loads(archive.read("data.json"))
            return result

    def erased(self, identity: ResourceId) -> None:
        self.assertFalse((self.storage.directory / (identity + ".sealed")).exists())
        self.assertTrue((self.storage.directory / (identity + ".revoked")).exists())


class ExportTests(ExportFixture):
    def test_authenticated_zip_public_projection_owner_admin_and_manifest(self) -> None:
        self.message()
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}/messages/{SOURCE}"].update(
            created_by=USER, model_message={"secret": "INTERNAL_REASONING"}, token="TOKEN_SECRET"
        )
        self.create_memory()
        private = copy.deepcopy(self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"])
        private.update(
            id=str(uuid4()),
            visibility="private",
            owner_user_id="someone-else",
            body="OTHER_PRIVATE",
        )
        self.store.data[f"tenants/{TENANT}/notes/{private['id']}"] = private
        identity = self.ready()
        archive = self.archive(identity)
        kinds = {entry["kind"] for entry in archive["manifest"]}
        self.assertTrue({"facts", "entities", "notes", "conversations", "messages"} <= kinds)
        content = json.dumps(archive)
        for forbidden in (
            "INTERNAL_REASONING",
            "TOKEN_SECRET",
            "OTHER_PRIVATE",
            "model_message",
            "external_refs",
        ):
            self.assertNotIn(forbidden, content)
        for item in archive["manifest"]:
            self.assertIn("version", item)
            self.assertEqual(item["deletion_epoch"], 0)
        self.assertEqual(self.create_export(), identity)
        self.assertEqual(
            self.client.post(
                self.operations_url,
                json={"kind": "export", "scope": "my_data"},
                headers={"Idempotency-Key": "export-fixture-key-001"},
            ).status_code,
            409,
        )
        sealed = (self.storage.directory / (identity + ".sealed")).read_bytes()
        self.assertNotIn(b"synthetic source", sealed)
        self.assertNotIn(b"data.json", sealed)

    def test_other_tenant_other_user_and_pending_download_denied_without_revoking_owner(
        self,
    ) -> None:
        identity = self.create_export()
        self.assertEqual(self.download(identity).status_code, 404)
        self.generate(identity)
        self.assertEqual(self.download(identity, OTHER).status_code, 404)
        self.uid = FirebaseUid("other-user")
        self.assertEqual(self.download(identity).status_code, 404)
        self.uid = USER
        self.assertEqual(self.download(identity).status_code, 200)

    def test_generation_rechecks_membership_and_scope_change_erases_without_download(self) -> None:
        identity = self.ready()
        self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]["visibility"] = "private"
        self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]["owner_user_id"] = "someone-else"
        self.reaper.step()
        self.erased(identity)
        self.assertEqual(self.download(identity).status_code, 404)
        self.assertEqual(self.store.data[registry_path(identity)]["state"], "revoked")

    def test_forget_after_generation_immediately_invalidates_old_artifact(self) -> None:
        self.message()
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}/messages/{SOURCE}"][
            "created_by"
        ] = USER
        memory = self.create_memory(
            source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}]
        ).json()["id"]
        identity = self.ready()
        operation = self.memories.forget(
            self.identity(),
            TENANT,
            ForgetCreate(kind="forget_memory", target_id=memory, expected_version=1),
            "forget-export-fixture",
        )
        self.assertEqual(self.download(identity).status_code, 404)
        self.erased(identity)
        intent = self.memories.deletion_ledger.get(operation.operation_id)  # type: ignore[union-attr]
        assert intent is not None
        self.exports.invalidate(intent, key=f"{intent.operation_id}:export:v1")
        self.assertEqual(
            self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}/messages/{SOURCE}"][
                "text"
            ],
            "synthetic source",
        )

    def test_delayed_put_after_purge_receipt_cannot_resurrect(self) -> None:
        identity = self.create_export()
        put = self.storage.put

        def delayed(key: ResourceId, content: bytes, artifact: Artifact) -> None:
            self.exports.revoke(key)
            put(key, content, artifact)

        with patch.object(self.storage, "put", side_effect=delayed):
            self.generate(identity)
        self.erased(identity)
        self.assertEqual(self.download(identity).status_code, 404)
        restarted = LocalExportObjects(self.storage.directory, b"x" * 32, now=lambda: self.now)
        with self.assertRaises(ObjectRevoked):
            restarted.get(identity)

    def test_delayed_get_and_each_stream_boundary_rechecks_revocation(self) -> None:
        identity = self.ready()
        get = self.storage.get

        def delayed(key: ResourceId) -> tuple[bytes, Artifact]:
            result = get(key)
            self.store.data[purge_barrier_path(TENANT)] = {"epoch": 1}
            return result

        with patch.object(self.storage, "get", side_effect=delayed):
            self.assertEqual(self.download(identity).status_code, 404)
        self.erased(identity)

    def test_expiry_operation_object_and_expired_token(self) -> None:
        identity = self.ready()
        original = self.identity()
        self.now = NOW + timedelta(hours=1)
        with self.assertRaises(APIError):
            self.requests.download(original, TENANT, identity)
        self.assertTrue((self.storage.directory / (identity + ".sealed")).exists())
        self.now = NOW + timedelta(hours=24)
        self.reaper.step()
        self.erased(identity)
        with self.assertRaises(ObjectRevoked):
            self.storage.get(identity)

    def test_operation_deadline_independent_of_object_deadline(self) -> None:
        identity = self.ready()
        self.store.data[operation_path(TENANT, identity)]["operation"]["download_expires_at"] = (
            NOW.isoformat()
        )
        self.assertEqual(self.download(identity).status_code, 404)
        self.erased(identity)

    def test_cleanup_survives_tenant_records_removed_and_covers_every_lifecycle_kind(self) -> None:
        for kind in ("leave_tenant", "delete_account", "delete_tenant"):
            with self.subTest(kind=kind):
                identity = self.create_export(key="export-lifecycle-" + kind)
                self.generate(identity)
                self.assertEqual(self.download(identity).status_code, 200)
                self.store.data.pop(export_path(TENANT, identity))
                self.store.data.pop(operation_path(TENANT, identity))
                subject = subject_id(USER)
                anchor = subject if kind == "delete_account" else TENANT
                scopes = (
                    (LifecycleScope(kind="account", resource_id=subject, version=1),)
                    if kind == "delete_account"
                    else (
                        LifecycleScope(
                            kind="tenant" if kind == "delete_tenant" else "member",
                            resource_id=TENANT if kind == "delete_tenant" else subject,
                            tenant_id=TENANT,
                            version=1,
                        ),
                    )
                )
                intent = DeletionIntent(
                    kind=kind,
                    operation_id=ResourceId(str(uuid4())),
                    tenant_id=anchor,
                    request_hash="a" * 64,
                    source_policy="delete_source",
                    created_at=NOW,
                    targets=(),
                    scopes=scopes,
                )
                self.exports.invalidate(intent, key=f"{intent.operation_id}:export:v1")
                self.erased(identity)

    def test_object_expiry_is_enforced_independently(self) -> None:
        identity = self.ready()
        self.now += timedelta(days=1)
        with self.assertRaises(ObjectRevoked):
            self.storage.get(identity)

    def test_no_production_backend_is_silently_selected(self) -> None:
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            memories=self.memories
        )
        response = self.client.post(
            self.operations_url,
            json={"kind": "export", "scope": "my_data"},
            headers={"Idempotency-Key": "export-disabled-backend"},
        )
        self.assertEqual(response.status_code, 503)

    def test_expiry_after_slow_database_read_and_after_object_get_releases_no_bytes(self) -> None:
        identity = self.ready()
        original = self.identity()

        def expire(path: str) -> None:
            if path == registry_path(identity):
                self.now = original.expires_at

        self.store.after_get = expire
        with self.assertRaises(APIError):
            self.requests.download(original, TENANT, identity)
        self.store.after_get = lambda path: None
        self.now = NOW
        get = self.storage.get

        def late(key: ResourceId) -> tuple[bytes, Artifact]:
            result = get(key)
            self.now = original.expires_at
            return result

        with patch.object(self.storage, "get", side_effect=late), self.assertRaises(APIError):
            self.requests.download(original, TENANT, identity)

    def test_revocation_between_http_chunks_aborts_remaining_body(self) -> None:
        identity = self.ready()
        content = self.requests.download(self.identity(), TENANT, identity)
        response = ExportResponse(content * 500, self.requests, self.identity(), TENANT, identity)
        bodies: list[bytes] = []

        async def send(message: Any) -> None:
            if message["type"] == "http.response.body":
                bodies.append(message["body"])
                self.store.data[purge_barrier_path(TENANT)] = {"epoch": 1}

        async def receive() -> Any:
            return {"type": "http.disconnect"}

        with self.assertRaises(APIError):
            asyncio.run(response({}, receive, send))
        self.assertEqual(len(bodies), 1)
        self.assertEqual(len(bodies[0]), 65536)
        self.erased(identity)

    def test_slow_put_expired_lease_and_unknown_put_outcome_are_cleaned(self) -> None:
        identity = self.create_export()
        put = self.storage.put

        def delayed(key: ResourceId, content: bytes, artifact: Artifact) -> None:
            put(key, content, artifact)
            self.now += timedelta(minutes=2)

        with patch.object(self.storage, "put", side_effect=delayed):
            self.generate(identity)
        self.erased(identity)
        self.assertEqual(self.download(identity).status_code, 404)

    def test_data_version_edit_and_source_parent_change_invalidate_manifest(self) -> None:
        self.message()
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}/messages/{SOURCE}"][
            "created_by"
        ] = USER
        self.create_memory(source_refs=[{"kind": "message", "resource_id": SOURCE, "version": 1}])
        identity = self.ready()
        self.store.data[f"tenants/{TENANT}/conversations/{CONVERSATION}"]["display_policy"] = (
            "never"
        )
        self.assertEqual(self.download(identity).status_code, 404)
        self.erased(identity)

    def test_scan_and_manifest_bounds_fail_without_partial_publication(self) -> None:
        identity = self.create_export()
        with patch("brainoid_worker.export_data.MAX_ITEMS", 1):
            self.generate(identity)
        self.assertEqual(self.download(identity).status_code, 404)
        self.erased(identity)

    def test_reaper_retries_failed_erasure_after_restart(self) -> None:
        identity = self.ready()
        self.store.data[purge_barrier_path(TENANT)] = {"epoch": 1}
        with patch.object(self.storage, "revoke", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.reaper.step()
        self.assertEqual(self.store.data[registry_path(identity)]["state"], "revoked")
        ExportReaper(self.exports).step()
        self.erased(identity)
        self.assertTrue(self.store.data[registry_path(identity)]["erased"])

    def test_purge_worker_uses_export_hook_and_waits_for_physical_erasure(self) -> None:
        from brainoid_worker.purge_authority import KIND as PURGE_KIND
        from brainoid_worker.purge_authority import PurgeJobs
        from brainoid_worker.purge_resource import NoExternalCopies, PurgeResource

        memory = self.create_memory().json()["id"]
        identity = self.ready()
        operation = self.memories.forget(
            self.identity(),
            TENANT,
            ForgetCreate(kind="forget_memory", target_id=memory, expected_version=1),
            "purge-with-export-backend",
        )
        assert self.memories.deletion_ledger is not None
        jobs = PurgeJobs(self.exports.jobs, self.memories.deletion_ledger)
        handler = PurgeResource(
            jobs, self.pages, hooks={"cache": NoExternalCopies(), "export": self.exports}
        )
        job = job_id(TENANT, PURGE_KIND, operation.operation_id)
        jobs.activate(TENANT, job)
        delivery = jobs.prepare_dispatch(TENANT, job, queue_name=PURGE_KIND)
        assert delivery is not None
        Worker(jobs, LocalCaller(), {PURGE_KIND: handler}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        self.assertEqual(
            self.store.data[operation_path(TENANT, operation.operation_id)]["operation"]["status"],
            "succeeded",
        )
        self.erased(identity)

    def test_restart_and_concurrent_revoke_are_permanent_for_late_put(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        identity = self.ready()
        content, artifact = self.storage.get(identity)
        restarted = LocalExportObjects(self.storage.directory, b"x" * 32, now=lambda: self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            erased = pool.submit(self.exports.revoke, identity)
            late = pool.submit(restarted.put, identity, content, artifact)
            erased.result()
            try:
                late.result()
            except ObjectRevoked:
                pass
        with self.assertRaises(ObjectRevoked):
            restarted.put(identity, content, artifact)
        self.erased(identity)

    def test_my_data_excludes_shared_contributions_by_other_people(self) -> None:
        self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"].update(
            created_by="someone-else", owner_user_id=None
        )
        identity = self.create_export(scope="my_data")
        self.generate(identity)
        self.assertNotIn("notes", {e["kind"] for e in self.archive(identity)["manifest"]})

    def test_admin_also_excludes_other_private_conversation_and_memory(self) -> None:
        self.message(visibility="private", owner_user_id="someone-else", display_policy="never")
        self.create_memory(visibility="private", display_policy="never")
        fact = next(d for p, d in self.store.data.items() if "/facts/" in p)
        fact["owner_user_id"] = "someone-else"
        membership = next(
            d for p, d in self.store.data.items() if "/memberships/" in p and d["user_id"] == USER
        )
        membership["role"] = "admin"
        identity = self.ready()
        archive = self.archive(identity)
        self.assertFalse(
            {"conversations", "messages", "facts"} & {e["kind"] for e in archive["manifest"]}
        )


class ExportLifecycleTests(DeletionFixture):
    def test_real_leave_and_account_acceptance_deny_old_url_and_purge_erases(self) -> None:
        from brainoid_api.common.pagination import Cursors
        from brainoid_api.repositories.tenants import Tenants
        from brainoid_worker.entry import Worker

        for account in (False, True):
            with self.subTest(account=account):
                self.setUp()
                temporary = Path(self.enterContext(tempfile.TemporaryDirectory())) / "temporary"
                objects = LocalExportObjects(temporary, b"l" * 32, now=lambda: self.now)
                exports = Exports(
                    self.normal,
                    objects,
                    FakePurgePages(self.store),
                    CanonicalSources(SavedMessages()),
                    now=lambda: self.now,
                )
                tenants = Tenants(
                    self.store,
                    Cursors(b"x" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                )
                requests = ExportRequests(exports, tenants)
                created = requests.create(
                    self.identity(),
                    self.tenant,
                    ExportCreate(kind="export", scope="visible_tenant_data"),
                    "real-lifecycle-export",
                )
                delivery = self.normal.prepare_dispatch(
                    self.tenant, job_id(self.tenant, KIND, created.operation_id)
                )
                assert delivery is not None
                Worker(self.normal, LocalCaller(), {KIND: ExportData(exports)}).handle(
                    LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
                )
                self.assertTrue(
                    requests.download(self.identity(), self.tenant, created.operation_id)
                )
                deleted = self.accept(account=account)
                self.assertEqual(
                    self.repo.get(
                        self.identity(), deleted.operation_id, None if account else self.tenant
                    ),
                    deleted,
                )
                with self.assertRaises(APIError):
                    requests.download(self.identity(), self.tenant, created.operation_id)
                self.handler.hooks["export"] = exports
                self.drain(deleted, subject_id(self.identity().user_id) if account else self.tenant)
                self.assertFalse((temporary / (created.operation_id + ".sealed")).exists())


class ExportConversationTests(TurnRunFixture):
    def test_real_conversation_producer_and_signed_assistant_have_public_projection_only(
        self,
    ) -> None:
        conversation = self.conversation(private=True)
        accepted = self.turn(conversation)
        self.execute(ResourceId(accepted.json()["run_id"]))
        directory = Path(self.enterContext(tempfile.TemporaryDirectory())) / "exports"
        objects = LocalExportObjects(directory, b"c" * 32, now=self.clock.now)
        exports = Exports(
            self.jobs,
            objects,
            FakePurgePages(self.store),  # type: ignore[arg-type]
            CanonicalSources(SavedMessages()),
            now=self.clock.now,
        )
        requests = ExportRequests(exports, self.tenants)
        operation = requests.create(
            self.identity(),
            self.tenant,
            ExportCreate(kind="export", scope="my_data"),
            "real-conversation-export",
        )
        delivery = self.jobs.prepare_dispatch(
            self.tenant, job_id(self.tenant, KIND, operation.operation_id)
        )
        assert delivery is not None
        Worker(self.jobs, LocalCaller(), {KIND: ExportData(exports)}).handle(
            LOCAL_AUTHORIZATION, delivery.model_dump(mode="json")
        )
        content = requests.download(self.identity(), self.tenant, operation.operation_id)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            body = json.loads(archive.read("data.json"))
        messages = [r["data"] for r in body["records"] if r["kind"] == "messages"]
        self.assertEqual({m["role"] for m in messages}, {"user", "assistant"})
        self.assertNotIn("PRIVATE_SIGNATURE", json.dumps(body))
        self.assertNotIn("model_message", json.dumps(body))

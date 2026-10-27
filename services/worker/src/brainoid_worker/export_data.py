"""Ordinary user-authorized Jobs handler; external bytes never enter a transaction."""

import hashlib
import io
import json
import sys
import zipfile

from brainoid_contracts import ResourceId
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, LeaseLost, RetryableError
from brainoid_memory.export_manifest import MAX_BYTES, MAX_ITEMS, MAX_SCAN, Artifact, ManifestEntry
from brainoid_memory.export_projection import project
from brainoid_memory.exports import Exports, registry_path
from brainoid_memory.forget import operation_path
from brainoid_memory.repository import FactTransaction, VerifiedCaller
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from pydantic import JsonValue

from brainoid_worker.entry import Execution


class ExportData:
    def __init__(self, exports: Exports) -> None:
        self.exports = exports

    def access(self, tx: Transaction, execution: Execution) -> FactTransaction:
        job = self.exports.jobs._live(tx, execution.lease)
        if job is None or job.lease_expires_at is None:
            raise LeaseLost()
        deadline = job.lease_expires_at

        def live() -> None:
            if self.exports.now() >= deadline:
                raise LeaseLost()

        protect(tx, live)

        def caller(tx: Transaction, tenant: ResourceId) -> VerifiedCaller:
            return VerifiedCaller(
                subject=job.grant.principal.subject, valid_from=job.created_at, valid_to=deadline
            )

        return self.exports.access(tx, job.tenant_id, caller)

    def run(self, execution: Execution) -> JsonValue:
        try:
            return self._run(execution)
        except PolicyUnavailable:
            # Canonical source reads use policy errors, while Worker only defers
            # the Jobs infrastructure signal. Never turn a ledger outage into a
            # confirmed denial and permanently tombstone a valid export key.
            raise AuthorizationUnavailable() from None

    def _run(self, execution: Execution) -> JsonValue:
        job = execution.check()
        tenant, identity = job.tenant_id, job.operation_id
        entries: list[ManifestEntry] = []
        records: list[Document] = []
        scanned = 0
        collections = [
            f"tenants/{tenant}/{kind}"
            for kind in (
                "operations",
                "conversations",
                "entities",
                "facts",
                "notes",
                "tasks",
                "reminders",
                "notifications",
                "commands",
                "integrations",
                "calendar_proposals",
            )
        ]
        while collections:
            collection = collections.pop(0)
            token = ""
            while True:
                execution.check(renew=True)
                paths, token = self.exports.pages.children(
                    collection, collections=False, token=token, limit=50
                )
                scanned += len(paths)
                if scanned > MAX_SCAN:
                    raise ValueError("Export scan limit exceeded")
                for path in paths:
                    if path.rsplit("/", 1)[0] != collection:
                        raise ValueError("Invalid export discovery path")

                    def read(
                        tx: Transaction, path: str = path, collection: str = collection
                    ) -> tuple[ManifestEntry, Document] | None:
                        access = self.access(tx, execution)
                        _, registry = self.exports.current(tx, identity, access, manifest=False)
                        if collection == f"tenants/{tenant}/operations":
                            operation = tx.get(path)
                            # Wait for physical purge so post-forget generated history
                            # cannot be recaptured while it is awaiting erasure.
                            if (
                                operation is not None
                                and operation.get("operation", {}).get("kind")
                                in ("forget_memory", "delete_resource")
                                and operation["operation"]["status"] != "succeeded"
                            ):
                                raise RetryableError()
                            return None
                        try:
                            return project(tx, access, path, scope=registry["export_scope"])
                        except PolicyUnavailable:
                            raise
                        except PolicyDenied:
                            return None

                    item = self.exports.store.run(read)
                    if item is not None:
                        entry, value = item
                        entries.append(entry)
                        records.append({"kind": entry.kind, "data": value})
                        if len(entries) > MAX_ITEMS:
                            raise ValueError("Export manifest limit exceeded")
                        if entry.kind == "conversations":
                            collections.append(path + "/messages")
                if not token:
                    break
        payload = json.dumps(
            {
                "schema_version": 1,
                "tenant_id": tenant,
                "operation_id": identity,
                "manifest": [e.model_dump(mode="json") for e in entries],
                "records": records,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(payload) > MAX_BYTES:
            raise ValueError("Export size limit exceeded")
        buffer = io.BytesIO()
        # ZIP_STORED avoids decompression bombs and puts an explicit JSON member
        # behind the OpenAPI application/zip contract. Stable timestamp aids replay.
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(zipfile.ZipInfo("data.json", date_time=(1980, 1, 1, 0, 0, 0)), payload)
        content = buffer.getvalue()

        def stage(tx: Transaction) -> Artifact:
            access = self.access(tx, execution)
            saved, registry = self.exports.current(tx, identity, access, manifest=False)
            for entry in entries:
                current = project(tx, access, entry.path(tenant), scope=registry["export_scope"])
                if current is None or current[0] != entry:
                    raise PolicyDenied()
            artifact = Artifact(
                expires_at=registry["expires_at"],
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
            tx.set(
                registry_path(identity),
                {
                    **registry,
                    "state": "staged",
                    "manifest": [e.model_dump(mode="json") for e in entries],
                    "artifact": artifact.model_dump(mode="json"),
                },
            )
            tx.set(
                operation_path(tenant, identity),
                {
                    **saved,
                    "operation": {
                        **saved["operation"],
                        "status": "running",
                        "updated_at": self.exports.now().isoformat(),
                    },
                },
            )
            return artifact

        artifact = self.exports.store.run(stage)

        def check(tx: Transaction) -> None:
            self.exports.current(tx, identity, self.access(tx, execution))

        def put(key: str) -> JsonValue:
            self.exports.store.run(check)
            try:
                self.exports.objects.put(identity, content, artifact)
                self.exports.store.run(check)
            except (PolicyUnavailable, AuthorizationUnavailable, PersistenceUnavailable):
                try:
                    self.exports.revoke(identity)
                except Exception:
                    # Preserve the infrastructure signal. This effect has already
                    # started, so Jobs fences it as outcome_unknown; the durable
                    # registry lets the reaper retry any unfinished erasure.
                    pass
                raise
            except Exception:
                self.exports.revoke(identity)
                raise
            return artifact.model_dump(mode="json")

        return execution.effect("private-export-v1", put)

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        try:
            self._commit(tx, job, result)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None

    def _commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        from brainoid_jobs import Lease

        execution = Execution(
            self.exports.jobs,
            Lease(tenant_id=job.tenant_id, job_id=job.id, generation=job.lease_generation),
        )
        access = self.access(tx, execution)
        saved, registry = self.exports.current(tx, job.operation_id, access)
        if registry.get("artifact") != result or registry["state"] != "staged":
            raise PolicyDenied()
        tx.set(registry_path(job.operation_id), {**registry, "state": "ready"})
        tx.set(
            operation_path(job.tenant_id, job.operation_id),
            {
                **saved,
                "operation": {
                    **saved["operation"],
                    "status": "succeeded",
                    "updated_at": self.exports.now().isoformat(),
                },
            },
        )

    def reconcile(self, job: Job) -> None:
        pending = sys.exception()
        try:
            self.exports.cleanup_one(job.operation_id)
        except Exception as error:
            if isinstance(pending, (AuthorizationUnavailable, PersistenceUnavailable)):
                # Called from Worker's finally: cleanup must not replace the
                # original infrastructure signal before defer_unavailable refunds
                # the attempt (or fences an unresolved started effect). The durable
                # artifact registry lets the independent reaper retry cleanup.
                return
            if isinstance(error, PolicyUnavailable):
                raise AuthorizationUnavailable() from None
            raise

"""Export admission, canonical manifest fences, and durable temporary-copy cleanup."""

import hashlib
from datetime import timedelta
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import purge_epoch, subject_id
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import GuardedTransactions, ReleaseGuard, protect, release_guard
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job, Jobs, RetryableError
from brainoid_jobs.models import TERMINAL, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.models import Action, ResourceKind, ResourceRef, UserSubject
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, ResourceGrant
from brainoid_policy.transactions import resource_state

from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.export_manifest import (
    KIND,
    MAX_ITEMS,
    Artifact,
    Authorize,
    Clock,
    ExportCreate,
    ExportObjects,
    ExportPages,
    ManifestEntry,
)
from brainoid_memory.export_projection import project
from brainoid_memory.forget import Operation, operation_path
from brainoid_memory.repository import FactTransaction, MemoryRepository, VerifiedCaller
from brainoid_memory.sources import SourceResolver


def export_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/exports/{identity}"


def registry_path(identity: ResourceId) -> str:
    return f"export_artifacts/{identity}"


class _Caller:
    def __init__(self, authorize: Authorize) -> None:
        self.authorize_fn = authorize

    def authorize(self, tx: Transaction, context: object) -> VerifiedCaller:
        from brainoid_contracts import TenantContext

        assert isinstance(context, TenantContext)
        return self.authorize_fn(tx, context.tenant_id)


class Exports:
    def __init__(
        self,
        jobs: Jobs,
        objects: ExportObjects,
        pages: ExportPages,
        sources: SourceResolver,
        *,
        now: Clock,
        credential_epoch: ResourceId | None = None,
    ) -> None:
        self.jobs, self.objects, self.pages, self.sources, self.now = (
            jobs,
            objects,
            pages,
            sources,
            now,
        )
        # API and handler share a guarded store; no-I/O deadline checks survive commit.
        if not isinstance(jobs.store, GuardedTransactions):
            jobs.store = GuardedTransactions(jobs.store)
        self.store = jobs.store
        self.credential_epoch = credential_epoch

    def access(self, tx: Transaction, tenant: ResourceId, authorize: Authorize) -> FactTransaction:
        caller = authorize(tx, tenant)
        if not isinstance(caller.subject, UserSubject):
            raise PolicyDenied()
        principal = self.jobs._policy(tx).principal(tenant, caller.subject)
        repository = MemoryRepository(
            self.store,
            authorizer=_Caller(authorize),
            credential_epoch=self.credential_epoch,
            sources=self.sources,
            ledger=self.jobs.ledger,
            now=self.now,
            allow_untracked_bootstrap=self.jobs.allow_untracked,
        )
        return repository.facts(tx, principal.context)

    def create(
        self, tenant: ResourceId, body: ExportCreate, key: str | None, authorize: Authorize
    ) -> Operation:
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)

        def save(tx: Transaction) -> Operation:
            access = self.access(tx, tenant, authorize)
            user = access.principal.user_id
            assert user is not None
            identity = ResourceId(str(uuid5(UUID(tenant), fingerprint(["export", user, key]))))
            op_path = operation_path(tenant, identity)
            existing = tx.get(op_path)
            if existing is not None:
                if (
                    existing.get("export_scope") != body.scope
                    or existing.get("requested_by") != user
                ):
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return Operation.model_validate(existing["operation"])
            if (
                tx.get(export_path(tenant, identity)) is not None
                or tx.get(registry_path(identity)) is not None
            ):
                raise PolicyDenied()
            now = self.now()
            expires = now + timedelta(hours=24)
            epoch = purge_epoch(tx, tenant)
            metadata = {
                "id": identity,
                "tenant_id": tenant,
                "created_by": user,
                "owner_user_id": user,
                "visibility": "private",
                "display_policy": "authenticated_session",
                "version": 1,
                "deletion_epoch": 0,
                "deleted": False,
                "suppress_reuse": False,
            }
            state = resource_state(
                ResourceRef(tenant_id=tenant, kind=ResourceKind.EXPORT, id=identity), metadata
            )
            assert state is not None
            grant = ResourceGrant(access.principal, state, Action.READ)
            job = Job(
                id=job_id(tenant, KIND, identity),
                tenant_id=tenant,
                operation_id=identity,
                kind=KIND,
                grant=grant,
                account_stamps={user: account_stamp(tx, user)},
                created_at=now,
                available_at=now,
                dispatch_after=now,
            )
            operation = Operation(
                operation_id=identity,
                kind="export",
                created_at=now,
                updated_at=now,
                download_expires_at=expires,
            )
            saved = {
                "tenant_id": tenant,
                "requested_by": user,
                "export_scope": body.scope,
                "operation": operation.model_dump(mode="json"),
            }
            # Registry is outside tenant deletion traversal, contains no plaintext,
            # and is durable BEFORE any put. It is the cleanup inventory after crashes.
            registry = {
                "id": identity,
                "tenant_id": tenant,
                "owner_user_id": user,
                "subject_id": subject_id(user),
                "state": "pending",
                "manifest": [],
                "purge_epoch": epoch,
                "expires_at": expires.isoformat(),
                "job": job.model_dump(mode="json"),
                "export_scope": body.scope,
            }
            access.finish()
            tx.create(export_path(tenant, identity), metadata)
            tx.create(op_path, saved)
            tx.create(registry_path(identity), registry)
            tx.create(job_path(tenant, job.id), job.model_dump(mode="json"))
            return operation

        return self.store.run(save)

    def current(
        self,
        tx: Transaction,
        identity: ResourceId,
        access: FactTransaction,
        *,
        ready: bool = False,
        manifest: bool = True,
    ) -> tuple[Document, Document]:
        tenant = access.principal.context.tenant_id
        saved = tx.get(operation_path(tenant, identity))
        registry = tx.get(registry_path(identity))
        if (
            saved is None
            or registry is None
            or (
                registry.get("id") != identity
                or registry.get("tenant_id") != tenant
                or registry.get("owner_user_id") != access.principal.user_id
                or saved.get("requested_by") != access.principal.user_id
                or saved.get("operation", {}).get("kind") != "export"
                or registry.get("export_scope") != saved.get("export_scope")
            )
        ):
            raise PolicyDenied()
        from brainoid_contracts import UtcTimestamp
        from pydantic import TypeAdapter

        expiry = TypeAdapter(UtcTimestamp).validate_python(registry["expires_at"])
        operation = Operation.model_validate(saved["operation"])

        def deadline() -> None:
            if (
                self.now() >= expiry
                or operation.download_expires_at is None
                or self.now() >= operation.download_expires_at
            ):
                raise PolicyDenied()

        protect(tx, deadline)
        if registry["state"] == "revoked" or (
            ready and (registry["state"] != "ready" or operation.status != "succeeded")
        ):
            raise PolicyDenied()
        job = Job.model_validate(registry["job"])
        if (
            job.operation_id != identity
            or job.tenant_id != tenant
            or job.grant.ref.kind != ResourceKind.EXPORT
            or job.grant.ref.id != identity
            or job.grant.principal.user_id != access.principal.user_id
        ):
            raise PolicyDenied()
        self.jobs._policy(tx).worker(job.grant)
        if self.jobs._accounts(tx, job.grant, None) != job.account_stamps:
            raise PolicyDenied()
        # A forget invalidates every earlier snapshot in this tenant. This also
        # fences indirect assistant copies and generation/forget transaction races.
        if registry["purge_epoch"] != purge_epoch(tx, tenant):
            raise PolicyDenied()
        entries = tuple(ManifestEntry.model_validate(v) for v in registry["manifest"])
        if len(entries) > MAX_ITEMS:
            raise PolicyDenied()
        if manifest:
            for entry in entries:
                current = project(tx, access, entry.path(tenant), scope=registry["export_scope"])
                if current is None or current[0] != entry:
                    raise PolicyDenied()
        access.finish()
        return saved, registry

    def get(self, tenant: ResourceId, identity: ResourceId, authorize: Authorize) -> Operation:
        def read(tx: Transaction) -> Operation:
            access = self.access(tx, tenant, authorize)
            saved = tx.get(operation_path(tenant, identity))
            if saved is None or saved.get("operation", {}).get("kind") != "export":
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            if saved.get("requested_by") != access.principal.user_id:
                raise PolicyDenied()
            return Operation.model_validate(saved["operation"])

        return self.store.run(read)

    def validate(
        self, tenant: ResourceId, identity: ResourceId, authorize: Authorize
    ) -> tuple[Document, ReleaseGuard]:
        def read(tx: Transaction) -> tuple[Document, ReleaseGuard]:
            access = self.access(tx, tenant, authorize)
            _, registry = self.current(tx, identity, access, ready=True)
            return registry, release_guard(tx)

        result, guard = self.store.run(read)
        guard.finish()
        return result, guard

    def download(self, tenant: ResourceId, identity: ResourceId, authorize: Authorize) -> bytes:
        registry, guard = self.validate(tenant, identity, authorize)
        content, artifact = self.objects.get(identity)
        guard.finish()
        latest, guard = self.validate(tenant, identity, authorize)
        if (
            latest != registry
            or artifact != Artifact.model_validate(registry["artifact"])
            or (
                hashlib.sha256(content).hexdigest() != artifact.sha256
                or len(content) != artifact.size
            )
        ):
            raise PolicyDenied()
        guard.finish()
        return content

    def revoke(self, identity: ResourceId) -> None:
        """Cleanup capability only: cannot read bytes, grant access, or create an object."""

        def mark(tx: Transaction) -> None:
            registry = tx.get(registry_path(identity))
            if registry is None:
                return
            tenant = ResourceId(registry["tenant_id"])
            saved = tx.get(operation_path(tenant, identity))
            metadata = tx.get(export_path(tenant, identity))
            tx.set(registry_path(identity), {**registry, "state": "revoked"})
            if metadata is not None and not metadata.get("deleted"):
                tx.set(export_path(tenant, identity), {**metadata, "deleted": True})
            if saved is not None and saved.get("operation", {}).get("kind") == "export":
                operation = {
                    **saved["operation"],
                    "status": "failed",
                    "error_code": "export_unavailable",
                    "updated_at": self.now().isoformat(),
                }
                tx.set(operation_path(tenant, identity), {**saved, "operation": operation})

        self.store.run(mark)
        # Storage revocation itself is durable and defeats a put already in flight.
        self.objects.revoke(identity)

        def receipt(tx: Transaction) -> None:
            registry = tx.get(registry_path(identity))
            if registry is not None:
                tx.set(registry_path(identity), {**registry, "erased": True})

        self.store.run(receipt)

    def cleanup_one(self, identity: ResourceId) -> bool:
        """Reaper rechecks saved delegation only to decide erasure, never to release data."""

        def valid(tx: Transaction) -> bool:
            registry = tx.get(registry_path(identity))
            if registry is None or registry.get("erased") is True:
                return True
            if registry.get("state") == "revoked":
                return False
            job = Job.model_validate(registry["job"])
            live = tx.get(job_path(job.tenant_id, job.id))
            if live is None or live.get("status") in (TERMINAL - {"succeeded"}):
                return False

            def caller(tx: Transaction, tenant: ResourceId) -> VerifiedCaller:
                # A bounded original delegation, used ONLY by this delete-only path.
                return VerifiedCaller(
                    subject=job.grant.principal.subject,
                    valid_from=job.created_at,
                    valid_to=registry["expires_at"],
                )

            access = self.access(tx, job.tenant_id, caller)
            self.current(tx, identity, access)
            return True

        try:
            if self.store.run(valid):
                return False
        except PolicyUnavailable:
            raise
        except (PolicyDenied, AccountUnavailable):
            pass
        self.revoke(identity)
        return True

    def sweep(self, *, token: str = "", limit: int = 50) -> str:
        """One bounded durable-inventory page; persist returned cursor in the scheduler.

        Empty cursor restarts a pass. Crashes can replay pages safely. This uses no
        normal job grant and continues after the exporting account/tenant is gone.
        """
        paths, next_token = self.pages.children(
            "export_artifacts", collections=False, token=token, limit=limit
        )
        for path in paths:
            identity = ResourceId(path.rsplit("/", 1)[1])
            if path != registry_path(identity):
                raise ValueError("Invalid export registry path")
            self.cleanup_one(identity)
        return next_token

    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        """Purge hook for every accepted intent; no tenant DB record is needed."""
        if key != f"{intent.operation_id}:export:v1":
            raise ValueError("Invalid export erasure key")
        progress_path = f"export_erasure/{intent.operation_id}"
        binding = fingerprint(intent.model_dump(mode="json"))
        progress = self.store.run(lambda tx: tx.get(progress_path))
        if progress is not None and progress.get("binding") != binding:
            raise ValueError("Export erasure operation conflict")
        if progress is not None and progress.get("done") is True:
            return fingerprint([key, "export-erased-v1"])
        token = "" if progress is None else progress["cursor"]
        boundary = None
        if intent.kind in ("forget_memory", "delete_resource"):
            accepted = self.store.run(
                lambda tx: tx.get(operation_path(intent.tenant_id, intent.operation_id))
            )
            boundary = None if accepted is None else accepted.get("purge_admission_epoch")
            if type(boundary) is not int:
                raise ValueError("Accepted forget export boundary required")
        # Bounded per invocation, with durable progress for the purge worker's retry.
        for _ in range(10):
            paths, token = self.pages.children(
                "export_artifacts", collections=False, token=token, limit=50
            )
            for path in paths:

                def read_registry(tx: Transaction, path: str = path) -> Document | None:
                    return tx.get(path)

                registry = self.store.run(read_registry)
                if registry is None:
                    continue
                original = Job.model_validate(registry["job"])
                selected = (
                    intent.kind in ("forget_memory", "delete_resource")
                    and registry["tenant_id"] == intent.tenant_id
                    and boundary is not None
                    and registry["purge_epoch"] < boundary
                ) or any(
                    (scope.kind == "account" and registry["subject_id"] == scope.resource_id)
                    or (scope.kind == "tenant" and registry["tenant_id"] == scope.tenant_id)
                    or (
                        scope.kind == "member"
                        and registry["tenant_id"] == scope.tenant_id
                        and registry["subject_id"] == scope.resource_id
                        and original.grant.context.actor.authorization_version <= scope.version
                    )
                    for scope in intent.scopes
                )
                if selected and registry.get("erased") is not True:
                    self.revoke(ResourceId(registry["id"]))

            def checkpoint(tx: Transaction, cursor: str = token) -> None:
                tx.set(progress_path, {"binding": binding, "cursor": cursor, "done": not cursor})

            self.store.run(checkpoint)
            if not token:
                return fingerprint([key, "export-erased-v1"])
        raise RetryableError()

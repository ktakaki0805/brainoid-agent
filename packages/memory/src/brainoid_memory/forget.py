"""Ledger-first, resumable forget acceptance shared by HTTP and typed tools."""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId, Scope, UtcTimestamp, Version, Visibility
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import tombstone_path
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_jobs.models import Job, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.accounts import account_stamp
from brainoid_policy.models import Action, ResourceKind, ResourceRef
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from pydantic import TypeAdapter, ValidationError

from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger, DeletionTarget
from brainoid_memory.models import Fact, MemoryValue
from brainoid_memory.repository import FactTransaction, VersionConflict, _stored


class ForgetCreate(MemoryValue):
    kind: Literal["forget_memory"]
    target_id: ResourceId
    expected_version: Version
    source_policy: Literal["suppress_reuse", "delete_source"] = "suppress_reuse"


class Operation(MemoryValue):
    operation_id: ResourceId
    kind: Literal["forget_memory"] = "forget_memory"
    status: Literal["pending", "running", "succeeded", "failed", "outcome_unknown"] = "pending"
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


def operation_path(tenant: ResourceId, operation: ResourceId) -> str:
    return f"tenants/{tenant}/operations/{operation}"


def reservation_path(tenant: ResourceId, target: ResourceId) -> str:
    return f"tenants/{tenant}/memory_forget_reservations/{target}"


def target_path(tenant: ResourceId, target: DeletionTarget) -> str:
    if target.kind == "message":
        if target.parent_id is None:
            raise PolicyDenied()
        return f"tenants/{tenant}/conversations/{target.parent_id}/messages/{target.resource_id}"
    return f"tenants/{tenant}/{'facts' if target.kind == 'fact' else 'notes'}/{target.resource_id}"


class ForgetMemories:
    def __init__(
        self, store: TransactionStore, ledger: DeletionLedger, *, now: Callable[[], datetime]
    ) -> None:
        self.store, self.ledger, self.now = store, ledger, now

    @staticmethod
    def operation(access: FactTransaction, document: Document | None) -> Operation:
        if document is None:
            raise PolicyDenied()
        principal = access.principal
        if (
            principal.user_id is None
            or document.get("requested_by") != principal.user_id
            or document.get("tenant_id") != principal.context.tenant_id
            or "memory.write" not in principal.context.actor.allowed_scopes
        ):
            raise PolicyDenied()
        scope = Scope.model_validate(document["scope"])
        if scope not in access.scopes():
            raise PolicyDenied()
        access.finish()
        return Operation.model_validate(document["operation"])

    def get(
        self, open_access: Callable[[Transaction], FactTransaction], identity: ResourceId
    ) -> Operation:
        def read(tx: Transaction) -> tuple[Operation, FactTransaction]:
            access = open_access(tx)
            return self.operation(
                access, tx.get(operation_path(access.principal.context.tenant_id, identity))
            ), access

        result, access = self.store.run(read)
        access.finish()
        return result

    def prepare(
        self, tx: Transaction, access: FactTransaction, body: ForgetCreate
    ) -> tuple[Fact, ResourceGrant, tuple[DeletionTarget, ...], list[tuple[str, Document]]]:
        """Read only. A reservation never substitutes for current authority."""
        tenant = access.principal.context.tenant_id
        ref = ResourceRef(tenant_id=tenant, kind=ResourceKind.MEMORY, id=body.target_id)
        grant = access._authority.policy.resource(
            tenant, access.caller.subject, ref, action=Action.WRITE, output=access._repo._output
        )
        try:
            record = Fact.model_validate(tx.get(f"tenants/{tenant}/facts/{body.target_id}"))
        except ValidationError:
            raise PolicyDenied() from None
        if record.version != body.expected_version or record.deletion_epoch != 0:
            raise VersionConflict()
        sources = []
        targets = [
            DeletionTarget(
                kind="fact", resource_id=record.id, version=record.version, deletion_epoch=1
            )
        ]
        for source in record.source_refs:
            if source.kind not in ("message", "note"):
                raise PolicyDenied()
            resolved = access._repo._sources.resolve(tx, access.principal.context, source)
            parent = access._authority.policy.resource(
                tenant, access.caller.subject, resolved.authority, action=Action.WRITE
            )
            # A private fact must not globally suppress a shared conversation/note.
            if parent.scope != grant.scope or (
                parent.scope.visibility == Visibility.PRIVATE
                and parent.owner_user_id != grant.owner_user_id
            ):
                raise PolicyDenied()
            target = DeletionTarget(
                kind="message" if source.kind == "message" else "note",
                resource_id=source.resource_id,
                parent_id=resolved.authority.id if source.kind == "message" else None,
                version=resolved.source.version,
                deletion_epoch=resolved.deletion_epoch + 1,
            )
            path = target_path(tenant, target)
            doc = tx.get(path)
            if doc is None or resolved.deleted:
                raise PolicyDenied()
            if body.source_policy == "delete_source" or source.kind == "message":
                # Tenant admin/owner is not the author of somebody else's source.
                author = doc.get("created_by")
                if source.kind == "message" and author is None:
                    # Older message schema binds authorship to its canonical run.
                    run_id = doc.get("run_id")
                    try:
                        run_id = TypeAdapter(ResourceId).validate_python(run_id)
                    except ValueError:
                        raise PolicyDenied() from None
                    run = tx.get(f"tenants/{tenant}/runs/{run_id}")
                    job_doc = tx.get(job_path(tenant, job_id(tenant, "run_turn", run_id)))
                    source_job = None if job_doc is None else Job.model_validate(job_doc)
                    author = (
                        source_job.grant.principal.user_id
                        if run is not None
                        and run.get("input_message_id") == source.resource_id
                        and source_job is not None
                        and source_job.grant.ref == resolved.authority
                        else None
                    )
                if author != access.principal.user_id:
                    raise PolicyDenied()
            targets.append(target)
            sources.append((path, doc))
        access.finish()
        return record, grant, tuple(targets), sources

    def forget(
        self,
        open_access: Callable[[Transaction], FactTransaction],
        body: ForgetCreate,
        key: str | None,
        *,
        effect: ResourceId | None = None,
        extra: Callable[[Transaction, Operation, dict[str, Document]], Callable[[], None]]
        | None = None,
    ) -> Operation:
        body = ForgetCreate.model_validate(body.model_dump())
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)

        def reserve(tx: Transaction) -> tuple[Document, FactTransaction]:
            access = open_access(tx)
            principal = access.principal
            if (
                principal.user_id is None
                or "memory.write" not in principal.context.actor.allowed_scopes
            ):
                raise PolicyDenied()
            tenant, actor = principal.context.tenant_id, principal.context.actor
            binding = fingerprint([actor.actor_type, actor.actor_id, "forget_memory", key])
            identity = effect or ResourceId(str(uuid5(UUID(tenant), binding)))
            request_hash = fingerprint([binding, body.model_dump(mode="json")])
            receipt = tx.get(operation_path(tenant, identity))
            reservation = tx.get(reservation_path(tenant, body.target_id))
            saved = receipt or reservation
            if saved is not None:
                self.operation(access, saved)
                if (
                    saved["request_hash"] != request_hash
                    or saved["operation"]["operation_id"] != identity
                ):
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                if receipt is None:
                    self.prepare(tx, access, body)
                return saved, access
            record, _, targets, sources = self.prepare(tx, access, body)
            for path, _ in sources:
                lock = tx.get(f"tenants/{tenant}/source_forget_reservations/{fingerprint(path)}")
                if lock is not None and lock.get("operation_id") != identity:
                    raise APIError(ErrorCode.CONFLICT)
            now = self.now()
            operation = Operation(operation_id=identity, created_at=now, updated_at=now)
            intent = DeletionIntent(
                operation_id=identity,
                tenant_id=tenant,
                request_hash=request_hash,
                source_policy=body.source_policy,
                created_at=now,
                targets=targets,
            )
            saved = {
                "request_hash": request_hash,
                "tenant_id": tenant,
                "requested_by": principal.user_id,
                "scope": Scope(
                    visibility=record.visibility, display_policy=record.display_policy
                ).model_dump(mode="json"),
                "operation": operation.model_dump(mode="json"),
                "intent": intent.model_dump(mode="json"),
            }
            access.finish()
            tx.create(reservation_path(tenant, body.target_id), saved)
            for path, _ in sources:
                tx.set(
                    f"tenants/{tenant}/source_forget_reservations/{fingerprint(path)}",
                    {"operation_id": identity},
                )
            return saved, access

        reserved, access = self.store.run(reserve)
        access.finish()
        intent = DeletionIntent.model_validate(reserved["intent"])
        previous = self.ledger.get(intent.operation_id)
        if previous is not None:
            # A restored DB may have lost its reservation timestamp. Only this
            # immutable request/target binding can replace the reconstructed time.
            if previous.model_copy(update={"created_at": intent.created_at}) != intent:
                raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
            intent = previous
        access.finish()  # A slow ledger lookup cannot extend the original authorization.
        self.ledger.append(intent)  # External I/O is never in a retryable transaction.
        access.finish()  # The original verified deadline still bounds this handoff.

        def commit(tx: Transaction) -> tuple[Operation, FactTransaction]:
            current = open_access(tx)
            tenant = current.principal.context.tenant_id
            existing = tx.get(operation_path(tenant, intent.operation_id))
            saved = tx.get(reservation_path(tenant, body.target_id))
            if existing is not None:
                if existing["request_hash"] != intent.request_hash:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return self.operation(current, existing), current
            if saved is None or saved["request_hash"] != intent.request_hash:
                raise PolicyDenied()
            self.operation(current, saved)
            record, grant, targets, sources = self.prepare(tx, current, body)
            for path, _ in sources:
                lock = tx.get(f"tenants/{tenant}/source_forget_reservations/{fingerprint(path)}")
                if lock is None or lock.get("operation_id") != intent.operation_id:
                    raise PolicyDenied()
            if targets != intent.targets:
                raise VersionConflict()
            tombstones = {}
            for target in targets:
                kind = {"fact": "facts", "message": "messages", "note": "notes"}[target.kind]
                path = tombstone_path(tenant, kind, target.resource_id, target.parent_id)
                existing_marker = tx.get(path)
                marker = {
                    "operation_id": intent.operation_id,
                    "target": target.model_dump(mode="json"),
                    "created_at": intent.created_at.isoformat(),
                    "mode": "delete"
                    if target.kind == "fact" or body.source_policy == "delete_source"
                    else "suppress_reuse",
                }
                if existing_marker is not None and existing_marker.get("mode") != "suppress_reuse":
                    raise PolicyDenied()
                tombstones[path] = (
                    marker if marker["mode"] == "delete" else existing_marker or marker
                )
            operation = Operation(
                operation_id=intent.operation_id,
                created_at=intent.created_at,
                updated_at=self.now(),
            )
            identity = job_id(tenant, "memory_purge", intent.operation_id)
            if tx.get(job_path(tenant, identity)) is not None:
                raise PolicyDenied()
            uid = current.principal.user_id
            assert uid is not None
            job = Job(
                id=identity,
                tenant_id=tenant,
                operation_id=intent.operation_id,
                kind="memory_purge",
                grant=grant,
                account_stamps={uid: account_stamp(tx, uid)},
                created_at=self.now(),
                available_at=self.now(),
                dispatch_after=self.now(),
                # MEM-08 owns a deletion-specific executor, not the regular resource
                # worker whose grants correctly reject a tombstoned target.
                status="waiting",
                attributes={"dispatch_queue": "memory_purge"},
            )
            replacements = {}
            for path, source in sources:
                replacement = {**source, "suppress_reuse": True, "version": source["version"] + 1}
                if body.source_policy == "delete_source":
                    replacement.update(deleted=True, deletion_epoch=source["deletion_epoch"] + 1)
                replacements[path] = replacement
            write_extra = extra(tx, operation, replacements) if extra else lambda: None
            updated = record.model_copy(
                update={
                    "deleted": True,
                    "suppress_reuse": True,
                    "deletion_epoch": 1,
                    "version": record.version + 1,
                    "updated_at": self.now(),
                }
            )
            current.finish()
            tx.set(f"tenants/{tenant}/facts/{record.id}", _stored(updated, mode="json"))
            for path, replacement in replacements.items():
                tx.set(path, replacement)
                tx.delete(f"tenants/{tenant}/source_forget_reservations/{fingerprint(path)}")
            for path, marker in tombstones.items():
                tx.set(path, marker)
            tx.create(job_path(tenant, identity), job.model_dump(mode="json"))
            tx.create(
                operation_path(tenant, intent.operation_id),
                {
                    **saved,
                    "intent": intent.model_dump(mode="json"),
                    "operation": operation.model_dump(mode="json"),
                },
            )
            write_extra()
            current.finish()
            return operation, current

        result, access = self.store.run(commit)
        access.finish()
        return result

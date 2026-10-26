"""Ledger-first lifecycle acceptance with exact-key retries and permanent fences."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import lifecycle_account_path, lifecycle_tenant_path, subject_id
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_jobs import Job
from brainoid_jobs.models import job_id
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger
from brainoid_memory.forget import Operation, operation_path
from brainoid_memory.lifecycle import KIND, audit_grant, marker, scope_fence
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.control_ledger import Change, ControlLedger, LedgerError, MemberTarget
from brainoid_policy.models import Role
from brainoid_policy.scopes import PolicyDenied
from brainoid_policy.transactions import TransactionPolicyState
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import to_jsonable_python

from brainoid_api.auth.context import AuthenticatedIdentity, require_recent_authentication
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.services.account_deletion import resolve_account
from brainoid_api.services.tenant_deletion import require_surviving_owner, resolve_tenant


class LeaveTenant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["leave_tenant"]


class DeleteTenant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["delete_tenant"]
    confirmation_name: str = Field(min_length=1, max_length=200)


class DeleteAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["delete_account"]
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Explicit boolean confirmation required")
        return value


type LifecycleCreate = LeaveTenant | DeleteTenant | DeleteAccount


class DeletionRequests:
    def __init__(
        self,
        store: TransactionStore,
        ledger: DeletionLedger,
        authz: ControlLedger,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store, self.ledger, self.authz, self.now = (
            GuardedTransactions(store),
            ledger,
            authz,
            now,
        )

    def _live(self, identity: AuthenticatedIdentity) -> None:
        if not identity.issued_at <= self.now() < identity.expires_at:
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)

    def get(
        self,
        identity: AuthenticatedIdentity,
        operation: ResourceId,
        tenant: ResourceId | None = None,
    ) -> Operation:
        self._live(identity)

        def read(tx: Transaction) -> Operation:
            protect(tx, lambda: self._live(identity))
            account = tx.get(lifecycle_account_path(identity.user_id))
            if account is not None and account.get("operation_id") != operation:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            request = tx.get(f"lifecycle_requests/{operation}")
            if (
                request is None
                or request.get("subject_id") != subject_id(identity.user_id)
                or request.get("endpoint_tenant") != tenant
            ):
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            saved = tx.get(operation_path(ResourceId(request["anchor"]), operation))
            if saved is None:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            intent = DeletionIntent.model_validate(saved["intent"])
            if self.ledger.get(operation) != intent or saved.get("request_hash") != request.get(
                "request_hash"
            ):
                raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE)
            if intent.kind == "delete_account" and saved.get("provider_finalized") is True:
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
            return Operation.model_validate(saved["operation"])

        try:
            return self.store.run(read)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

    def create(
        self,
        identity: AuthenticatedIdentity,
        body: LifecycleCreate,
        key: str | None,
        tenant: ResourceId | None = None,
    ) -> Operation:
        self._live(identity)
        require_recent_authentication(identity, now=self.now())
        if (
            key is None
            or not 16 <= len(key) <= 128
            or ((tenant is None) != isinstance(body, DeleteAccount))
        ):
            raise APIError(ErrorCode.INVALID_REQUEST)
        operation = ResourceId(str(uuid5(UUID(subject_id(identity.user_id)), "lifecycle:" + key)))
        request_hash = fingerprint([tenant, body.model_dump(mode="json")])
        request_path = f"lifecycle_requests/{operation}"

        def reserve(tx: Transaction) -> Document:
            protect(tx, lambda: require_recent_authentication(identity, now=self.now()))
            previous = tx.get(request_path)
            account = tx.get(lifecycle_account_path(identity.user_id))
            if account is not None and account.get("operation_id") != operation:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            if previous is not None:
                if (
                    previous.get("subject_id") != subject_id(identity.user_id)
                    or previous.get("request_hash") != request_hash
                    or previous.get("endpoint_tenant") != tenant
                ):
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return previous
            account_stamp(tx, identity.user_id)
            state = TransactionPolicyState(tx, ledger=self.authz, allow_untracked_bootstrap=True)
            if isinstance(body, DeleteAccount):
                scopes, snapshot = resolve_account(tx, state, identity.user_id)
                anchor = subject_id(identity.user_id)
            else:
                assert tenant is not None
                scope, snapshot = resolve_tenant(
                    tx,
                    state,
                    tenant,
                    identity.user_id,
                    delete=isinstance(body, DeleteTenant),
                    confirmation=body.confirmation_name if isinstance(body, DeleteTenant) else None,
                )
                scopes, anchor = (scope,), tenant
            intent = DeletionIntent(
                operation_id=operation,
                tenant_id=anchor,
                kind=body.kind,
                targets=(),
                scopes=scopes,
                request_hash=request_hash,
                source_policy="delete_source",
                created_at=self.now(),
            )
            # Reuse the immutable timestamp after a DB restore, never overwrite intent.
            old = self.ledger.get(operation)
            if old is not None:
                intent = intent.model_copy(update={"created_at": old.created_at})
                if intent != old:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
            changes = []
            if intent.kind != "delete_tenant":
                for path, doc in snapshot.items():
                    if "/memberships/" in path and doc.get("status") == "active":
                        changes.append(
                            Change(
                                operation_id=ResourceId(str(uuid5(UUID(operation), path))),
                                tenant_id=ResourceId(doc["tenant_id"]),
                                expected_version=doc["authorization_version"],
                                authorization_version=doc["authorization_version"] + 1,
                                effect="deny",
                                target=MemberTarget(
                                    user_id=identity.user_id,
                                    role=Role(doc["role"]),
                                    status="revoked",
                                ),
                            )
                        )
            saved = {
                "changes": [c.model_dump(mode="json") for c in changes],
                "subject_id": subject_id(identity.user_id),
                "endpoint_tenant": tenant,
                "anchor": anchor,
                "request_hash": request_hash,
                "intent": intent.model_dump(mode="json"),
                "invitation_epochs": {
                    p: d.get("invitation_epoch", 0)
                    for p, d in snapshot.items()
                    if p.startswith("tenants/") and len(p.split("/")) == 2
                },
                "snapshots": {
                    p: fingerprint(
                        to_jsonable_python(
                            {
                                k: v
                                for k, v in d.items()
                                if k not in {"membership_mutation", "invitation_epoch"}
                            }
                        )
                    )
                    if p.startswith("tenants/") and len(p.split("/")) == 2
                    else fingerprint(to_jsonable_python(d))
                    for p, d in snapshot.items()
                },
                "requested_by": identity.user_id,
            }
            # Every scope is resolved and every conflict checked before the first write.
            for scope in scopes:
                existing = tx.get(scope_fence(scope))
                if existing is not None and intent.kind == "leave_tenant":
                    old_operation = tx.get(
                        operation_path(anchor, ResourceId(existing["operation_id"]))
                    )
                    if (
                        old_operation is not None
                        and old_operation.get("operation", {}).get("status") == "succeeded"
                        and scope.version > existing.get("scope", {}).get("version", 0) + 1
                    ):
                        continue
                if existing is not None and not (
                    intent.kind == "delete_account"
                    and scope.kind == "member"
                    and existing.get("mode") == "delete"
                    and existing.get("scope", {}).get("resource_id") == scope.resource_id
                ):
                    raise APIError(ErrorCode.CONFLICT)
            tx.create(request_path, saved)
            for path, doc in snapshot.items():
                if path.startswith("tenants/") and len(path.split("/")) == 2:
                    tx.set(path, {**doc, "membership_mutation": operation})
            if isinstance(body, DeleteAccount):
                tx.create(
                    lifecycle_account_path(identity.user_id),
                    {"operation_id": operation, "mode": "reserved"},
                )
            return saved

        def validate(
            tx: Transaction, reserved: Document, intent: DeletionIntent
        ) -> dict[str, Document]:
            protect(tx, lambda: require_recent_authentication(identity, now=self.now()))
            self._live(identity)
            require_recent_authentication(identity, now=self.now())
            current = tx.get(request_path)
            if current != reserved:
                raise APIError(ErrorCode.CONFLICT)
            state = TransactionPolicyState(tx, ledger=self.authz, allow_untracked_bootstrap=True)
            replacements = {}
            for path, expected in reserved["snapshots"].items():
                doc = tx.get(path)
                if doc is None:
                    raise APIError(ErrorCode.CONFLICT)
                original = dict(doc)
                change = next(
                    (
                        Change.model_validate(c)
                        for c in reserved["changes"]
                        if c["tenant_id"] == doc.get("tenant_id") and "/memberships/" in path
                    ),
                    None,
                )
                if (
                    change is not None
                    and doc.get("authorization_change") == change.model_dump(mode="json")
                    and doc.get("authorization_operation_id") == change.operation_id
                    and doc.get("authorization_version") == change.authorization_version
                    and doc.get("status") in {"suspended", "revoked"}
                ):
                    replacements[path] = doc
                    continue
                if path.startswith("tenants/") and len(path.split("/")) == 2:
                    if original.pop("membership_mutation", None) != operation:
                        raise APIError(ErrorCode.CONFLICT)
                    change = next(
                        (
                            Change.model_validate(c)
                            for c in reserved["changes"]
                            if c["tenant_id"] == path.split("/")[1]
                        ),
                        None,
                    )
                    applied = False
                    if change is not None:
                        members = tx.find(
                            f"tenants/{change.tenant_id}/memberships", "user_id", identity.user_id
                        )
                        applied = (
                            len(members) == 1
                            and members[0].get("authorization_operation_id") == change.operation_id
                        )
                    if (
                        original.pop("invitation_epoch", 0)
                        != reserved["invitation_epochs"][path] + int(applied)
                        or fingerprint(to_jsonable_python(original)) != expected
                    ):
                        raise APIError(ErrorCode.CONFLICT)
                elif fingerprint(to_jsonable_python(original)) != expected:
                    raise APIError(ErrorCode.CONFLICT)
                replacements[path] = doc
            if intent.kind == "delete_account":
                if tx.get(lifecycle_account_path(identity.user_id)) != {
                    "operation_id": operation,
                    "mode": "reserved",
                }:
                    raise APIError(ErrorCode.CONFLICT)
                actual = {
                    p
                    for p, _ in tx.memberships(identity.user_id)
                    if tx.get(lifecycle_tenant_path(p.split("/")[1])) is None
                }
                expected = {p for p in reserved["snapshots"] if "/memberships/" in p}
                if actual != expected:
                    raise APIError(ErrorCode.CONFLICT)
            else:
                account_stamp(tx, identity.user_id)
            for scope in intent.scopes:
                if scope.kind == "member":
                    assert scope.tenant_id is not None
                    if tx.get(lifecycle_tenant_path(scope.tenant_id)) is None:
                        require_surviving_owner(tx, state, scope.tenant_id, identity.user_id)
                existing = tx.get(scope_fence(scope))
                if (
                    scope.kind != "account"
                    and existing is not None
                    and intent.kind != "delete_account"
                ):
                    previous = tx.get(
                        operation_path(intent.tenant_id, ResourceId(existing["operation_id"]))
                    )
                    if (
                        intent.kind != "leave_tenant"
                        or previous is None
                        or previous.get("operation", {}).get("status") != "succeeded"
                        or scope.version <= existing.get("scope", {}).get("version", 0) + 1
                    ):
                        raise APIError(ErrorCode.CONFLICT)
            return replacements

        def commit(tx: Transaction, reserved: Document, intent: DeletionIntent) -> Operation:
            protect(tx, lambda: require_recent_authentication(identity, now=self.now()))
            completed = tx.get(operation_path(intent.tenant_id, operation))
            if completed is not None:
                if completed.get("intent") != intent.model_dump(mode="json"):
                    raise APIError(ErrorCode.CONFLICT)
                if completed.get("provider_finalized") is True:
                    raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
                return Operation.model_validate(completed["operation"])
            replacements = validate(tx, reserved, intent)
            fences = {scope_fence(scope): tx.get(scope_fence(scope)) for scope in intent.scopes}
            result = Operation(
                operation_id=operation,
                kind=body.kind,
                created_at=intent.created_at,
                updated_at=self.now(),
            )
            job = Job(
                id=job_id(intent.tenant_id, KIND, operation),
                tenant_id=intent.tenant_id,
                operation_id=operation,
                kind=KIND,
                grant=audit_grant(intent.tenant_id, operation, identity.user_id),
                account_stamps={},
                created_at=self.now(),
                available_at=self.now(),
                dispatch_after=self.now(),
                status="waiting",
                attributes={"dispatch_queue": KIND},
            )
            if tx.get(f"tenants/{intent.tenant_id}/jobs/{job.id}") is not None:
                raise APIError(ErrorCode.CONFLICT)
            for path, doc in replacements.items():
                if len(path.split("/")) == 2 and path.startswith("tenants/"):
                    update = {**doc, "membership_mutation": None}
                    if intent.kind == "delete_tenant":
                        update.update(
                            status="deleting", deletion_epoch=intent.scopes[0].deletion_epoch
                        )
                    tx.set(path, update)
                elif "/memberships/" in path and intent.kind == "delete_tenant":
                    tx.set(path, {**doc, "status": "revoked"})
                elif path.startswith("users/"):
                    tx.set(path, {**doc, "status": "deleting", "version": doc["version"] + 1})
            for scope in intent.scopes:
                existing = fences[scope_fence(scope)]
                tx.set(
                    scope_fence(scope),
                    existing
                    if intent.kind == "delete_account"
                    and scope.kind == "member"
                    and existing is not None
                    else marker(intent, scope),
                )
            tx.create(
                operation_path(intent.tenant_id, operation),
                {
                    "tenant_id": intent.tenant_id,
                    "requested_by": identity.user_id,
                    "request_hash": request_hash,
                    "operation": result.model_dump(mode="json"),
                    "intent": intent.model_dump(mode="json"),
                    "profile_paths": [p for p in reserved["snapshots"] if p.startswith("users/")],
                },
            )
            tx.create(f"tenants/{intent.tenant_id}/jobs/{job.id}", job.model_dump(mode="json"))
            return result

        try:
            reserved = self.store.run(reserve)
            intent = DeletionIntent.model_validate(reserved["intent"])
            self.ledger.append(intent)
            completed = self.store.run(
                lambda tx: tx.get(operation_path(intent.tenant_id, operation))
            )
            if completed is None:
                for raw in reserved["changes"]:
                    change = Change.model_validate(raw)

                    def authorize(tx: Transaction, proposed: Change) -> None:
                        validate(tx, reserved, intent)

                    AuthzChanges(self.store, self.authz).execute(change, authorize=authorize)
            return self.store.run(lambda tx: commit(tx, reserved, intent))
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
        except (PolicyDenied, AccountUnavailable):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None

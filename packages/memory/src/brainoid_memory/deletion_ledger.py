"""Immutable deletion intentions, independent of the restorable application DB.

Only opaque references and counters are recorded: never fact/message text, account
identities or credentials. This protocol deliberately has no authorization grants.
"""

from typing import Annotated, Literal, Self

from brainoid_contracts import ResourceId, UtcTimestamp, Version
from brainoid_policy.control_ledger import LedgerConflict, LedgerError, Objects
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from brainoid_memory.models import MemoryValue


class DeletionTarget(MemoryValue):
    kind: Literal["fact", "message", "note", "task"]
    resource_id: ResourceId
    parent_id: ResourceId | None = None
    version: Version
    deletion_epoch: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def parent(self) -> Self:
        if (self.kind == "message") != (self.parent_id is not None):
            raise ValueError("Only messages require a canonical parent")
        return self


class LifecycleScope(MemoryValue):
    kind: Literal["account", "member", "tenant", "integration"]
    resource_id: ResourceId
    tenant_id: ResourceId | None = None
    version: Version
    deletion_epoch: Annotated[int, Field(strict=True, ge=1)] = 1
    authorization_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    secret_slots: tuple[ResourceId, ...] = Field(default=(), exclude_if=lambda v: not v)
    owner_subject_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)
    export_epoch: Annotated[int, Field(strict=True, ge=1)] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def binding(self) -> Self:
        if (self.kind == "account") != (self.tenant_id is None):
            raise ValueError("Invalid lifecycle scope")
        if self.kind == "tenant" and self.resource_id != self.tenant_id:
            raise ValueError("Invalid tenant scope")
        if self.kind == "integration":
            if (
                self.authorization_id is None
                or self.owner_subject_id is None
                or self.export_epoch is None
                or len(self.secret_slots) != 2
                or len(set(self.secret_slots)) != 2
            ):
                raise ValueError("Exact Calendar authorization and two secret slots required")
        elif (
            self.authorization_id is not None
            or self.owner_subject_id is not None
            or self.secret_slots
            or self.export_epoch is not None
        ):
            raise ValueError("Calendar bindings require an integration scope")
        return self


class DeletionIntent(MemoryValue):
    schema_version: Literal[1] = 1
    kind: Literal[
        "forget_memory",
        "leave_tenant",
        "delete_tenant",
        "delete_account",
        "delete_resource",
        "disconnect_calendar",
    ] = Field(default="forget_memory", exclude_if=lambda value: value == "forget_memory")
    include_derived_memories: bool = Field(default=True, exclude_if=lambda value: value is True)
    scopes: tuple[LifecycleScope, ...] = Field(default=(), exclude_if=lambda value: not value)
    operation_id: ResourceId
    tenant_id: ResourceId
    request_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_policy: Literal["suppress_reuse", "delete_source"]
    created_at: UtcTimestamp
    targets: Annotated[tuple[DeletionTarget, ...], Field(max_length=21)]

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if self.kind == "disconnect_calendar":
            if (
                self.targets
                or len(self.scopes) != 1
                or self.scopes[0].kind != "integration"
                or self.scopes[0].tenant_id != self.tenant_id
                or self.source_policy != "delete_source"
                or not self.include_derived_memories
            ):
                raise ValueError("Calendar disconnect requires one exact integration scope")
            return self
        if any(s.kind == "integration" for s in self.scopes):
            raise ValueError("Integration scope requires Calendar disconnect")
        if self.kind == "delete_resource":
            if (
                self.scopes
                or len(self.targets) != 1
                or self.targets[0].kind not in {"note", "task"}
                or self.source_policy != "delete_source"
            ):
                raise ValueError("Resource deletion requires one exact note/task target")
            return self
        if not self.include_derived_memories:
            raise ValueError("Derived option only applies to resource deletion")
        if self.kind != "forget_memory":
            if self.targets or not self.scopes or self.source_policy != "delete_source":
                raise ValueError("Lifecycle deletion has exact scopes, not source targets")
            scope_keys = [(s.kind, s.tenant_id, s.resource_id) for s in self.scopes]
            if len(scope_keys) != len(set(scope_keys)):
                raise ValueError("Duplicate lifecycle scope")
            if self.kind == "delete_tenant":
                if (
                    len(self.scopes) != 1
                    or self.scopes[0].kind != "tenant"
                    or self.scopes[0].tenant_id != self.tenant_id
                ):
                    raise ValueError("Single tenant required")
            elif self.kind == "leave_tenant":
                if (
                    len(self.scopes) != 1
                    or self.scopes[0].kind != "member"
                    or self.scopes[0].tenant_id != self.tenant_id
                ):
                    raise ValueError("Single membership required")
            elif (
                self.scopes[0].kind != "account"
                or self.scopes[0].resource_id != self.tenant_id
                or any(
                    s.kind != "member" or s.resource_id != self.tenant_id for s in self.scopes[1:]
                )
            ):
                raise ValueError("Account and its own memberships required")
            return self
        if self.scopes or not self.targets:
            raise ValueError("Forget requires targets only")
        keys = [(t.kind, t.parent_id, t.resource_id) for t in self.targets]
        if (
            self.targets[0].kind != "fact"
            or any(t.kind not in {"note", "message"} for t in self.targets[1:])
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("A forget intent has one fact and unique sources")
        return self


class DeletionLedger:
    def __init__(self, objects: Objects) -> None:
        self.objects = objects

    @staticmethod
    def name(operation: ResourceId) -> str:
        # Validate even if a caller bypassed the normal DTO constructor.
        identity = TypeAdapter(ResourceId).validate_python(operation)
        return f"deletion/v1/operations/{identity}.json"

    def get(self, operation: ResourceId) -> DeletionIntent | None:
        raw = self.objects.read(self.name(operation))
        if raw is None:
            return None
        try:
            intent = DeletionIntent.model_validate_json(raw)
        except ValidationError:
            raise LedgerError() from None
        if intent.operation_id != operation:
            raise LedgerError()
        return intent

    def append(self, intent: DeletionIntent) -> None:
        intent = DeletionIntent.model_validate(intent.model_dump())
        if not self.objects.create(
            self.name(intent.operation_id), intent.model_dump_json().encode()
        ):
            if self.get(intent.operation_id) != intent:
                raise LedgerConflict()

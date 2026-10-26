"""Canonical task state; due instants and explicit user transitions are persisted."""

from collections.abc import Callable
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, IanaTimezone, ResourceId, Scope, UtcTimestamp, Version
from brainoid_contracts.transactions import Transaction
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.models import ResourceKind
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field, model_validator

from brainoid_memory.direct_resources import DirectRecord, DirectResources
from brainoid_memory.models import MemoryValue
from brainoid_memory.notes import Title
from brainoid_memory.repository import FactTransaction

TaskStatus = Literal["open", "done", "cancelled"]


class TaskCreate(Scope):
    title: Title
    due_at: UtcTimestamp | None = Field(default=None, exclude_if=lambda v: v is None)
    timezone: IanaTimezone | None = Field(default=None, exclude_if=lambda v: v is None)
    assignee_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def optional_nonnull(self) -> Self:
        if any(getattr(self, k) is None for k in self.model_fields_set):
            raise ValueError("Omit absent create fields")
        return self


class TaskPatch(MemoryValue):
    title: Title | None = None
    status: TaskStatus | None = None
    due_at: UtcTimestamp | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set - {"due_at"}
        ):
            raise ValueError("Nonempty changes; only due_at accepts null")
        return self


class Task(DirectRecord):
    title: Title
    due_at: UtcTimestamp | None = None
    timezone: IanaTimezone | None = None
    assignee_user_id: FirebaseUid | None = None
    status: TaskStatus = "open"


class TaskView(Scope):
    id: ResourceId
    tenant_id: ResourceId
    title: Title
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    status: TaskStatus
    due_at: UtcTimestamp | None = Field(default=None, exclude_if=lambda v: v is None)
    timezone: IanaTimezone | None = Field(default=None, exclude_if=lambda v: v is None)
    assignee_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)


class TaskPage(MemoryValue):
    items: Annotated[list[TaskView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None = None


def view(record: Task) -> TaskView:
    return TaskView.model_validate({k: getattr(record, k) for k in TaskView.model_fields})


class Tasks(DirectResources[TaskCreate, TaskPatch, Task]):
    kind = ResourceKind.TASK
    create_type = TaskCreate
    patch_type = TaskPatch
    record_type = Task

    @staticmethod
    def prepare_write(tx: Transaction, old: Task | None, record: Task) -> Callable[[], None]:
        if old is None or old.status == record.status:
            return lambda: None
        identity = str(uuid5(UUID(record.id), f"task-status:{record.version}"))
        path = f"tenants/{record.tenant_id}/task_events/{identity}"
        if tx.get(path) is not None:
            raise PolicyDenied()
        event = {
            "id": record.id,
            "tenant_id": record.tenant_id,
            "version": record.version,
            "from_status": old.status,
            "status": record.status,
            "expected_version": old.version,
            "actor_user_id": record.updated_by,
            "via_device_id": record.updated_via_device_id,
            "created_at": record.updated_at.isoformat(),
            "visibility": record.visibility,
            "owner_user_id": record.owner_user_id,
            "created_by": record.created_by,
        }
        return lambda: tx.create(path, event)

    @classmethod
    def validate_body(cls, access: FactTransaction, body: TaskCreate | TaskPatch) -> None:
        if isinstance(body, TaskCreate) and body.assignee_user_id is not None:
            uid = body.assignee_user_id
            access._authority.policy._member(access.principal.context.tenant_id, uid)
            try:
                account_stamp(access._tx, uid)
            except AccountUnavailable:
                raise PolicyDenied() from None
        access.finish()


task_path = Tasks.path
effect_path = Tasks.effect_path

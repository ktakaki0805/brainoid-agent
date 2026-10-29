"""Device-local recovery DTOs, shipped in signed artifacts; API parity is tested."""

from typing import Annotated, Literal

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version
from pydantic import BaseModel, ConfigDict, Field, model_validator

SessionScope = Literal[
    "conversation",
    "memory.read",
    "memory.write",
    "notes",
    "tasks",
    "reminders",
    "calendar.read",
    "calendar.propose",
    "home.control",
]


class Record(BaseModel):
    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True, allow_inf_nan=False
    )


class PrivateSession(Record):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    user_id: FirebaseUid
    device_id: ResourceId
    allowed_scopes: Annotated[list[SessionScope], Field(min_length=1, max_length=9)]
    expires_at: UtcTimestamp
    idle_expires_at: UtcTimestamp
    status: Literal["active", "ended", "expired"]


class RunAssignment(Record):
    run_id: ResourceId
    conversation_id: ResourceId
    output_mode: Literal["text", "speech"] = "text"
    assignment_generation: Version
    last_event_seq: Annotated[int, Field(strict=True, ge=0)]
    private_session_id: ResourceId | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class DeviceSnapshot(Record):
    device_id: ResourceId
    tenant_id: ResourceId
    # Credential generation; WSS generation is deliberately separate.
    connection_generation: Version
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    active_private_session: PrivateSession | None
    assigned_runs: Annotated[list[RunAssignment], Field(max_length=20)]
    pending_command_ids: Annotated[list[ResourceId], Field(max_length=20)]
    pending_notification_ids: Annotated[list[ResourceId], Field(max_length=20)]
    delivery_cursor: Annotated[int, Field(strict=True, ge=0)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class NotificationView(Scope, Record):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    reminder_id: ResourceId
    title: Annotated[str, Field(min_length=1, max_length=200)]
    status: Literal["pending", "sent", "displayed", "acknowledged", "expired"]
    target_kind: Literal["user", "device"]
    target_id: FirebaseUid
    expires_at: UtcTimestamp


class SnapshotSelection(Record):
    snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    run_ids: Annotated[list[ResourceId], Field(max_length=128)]
    command_ids: Annotated[list[ResourceId], Field(max_length=128)]
    notification_ids: Annotated[list[ResourceId], Field(max_length=128)]

    @model_validator(mode="after")
    def bounded(self) -> "SnapshotSelection":
        groups = (self.run_ids, self.command_ids, self.notification_ids)
        if sum(map(len, groups)) > 128 or any(len(set(ids)) != len(ids) for ids in groups):
            raise ValueError("Invalid snapshot selection")
        return self


class DeviceSnapshotState(DeviceSnapshot):
    assigned_runs: Annotated[list[RunAssignment], Field(max_length=128)]
    pending_command_ids: Annotated[list[ResourceId], Field(max_length=128)]
    pending_notification_ids: Annotated[list[ResourceId], Field(max_length=128)]
    next_cursor: None = None

    @model_validator(mode="after")
    def bounded(self) -> "DeviceSnapshotState":
        if (
            len(self.assigned_runs)
            + len(self.pending_command_ids)
            + len(self.pending_notification_ids)
        ) > 128:
            raise ValueError("Invalid snapshot state")
        return self

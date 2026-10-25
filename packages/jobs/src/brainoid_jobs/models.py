"""Durable job envelopes. Payloads contain identifiers, never grants or business bodies."""

from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp
from brainoid_policy.models import Action, Record, Subject
from brainoid_policy.scopes import ResourceGrant
from pydantic import Field, JsonValue, model_validator

Counter = Annotated[int, Field(strict=True, ge=0)]
HandlerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
Reason = Literal[
    "authorization_changed",
    "authorization_unavailable",
    "persistence_unavailable",
    "source_changed",
    "source_deleted",
    "account_changed",
    "attempts_exhausted",
    "unknown_handler",
    "invalid_job",
    "handler_error",
    "outcome_unknown",
    "retry_requested",
    "lease_expired",
]
Status = Literal["pending", "running", "succeeded", "obsolete", "failed"]
TERMINAL = frozenset({"succeeded", "obsolete", "failed"})


def job_id(tenant_id: ResourceId, kind: str, operation_id: ResourceId) -> ResourceId:
    return ResourceId(str(uuid5(UUID(str(tenant_id)), f"{kind}:{operation_id}")))


class Delivery(Record):
    tenant_id: ResourceId
    job_id: ResourceId
    dispatch_generation: Annotated[int, Field(strict=True, ge=1)]

    @property
    def task_id(self) -> str:
        return f"{self.job_id}-d{self.dispatch_generation}"


class Checkpoint(Record):
    key: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_.-]{1,100}$")]
    state: Literal["started", "completed"]
    result: JsonValue = None


class Job(Record):
    schema_version: Literal[1] = 1
    id: ResourceId
    tenant_id: ResourceId
    operation_id: ResourceId
    kind: HandlerName
    grant: ResourceGrant
    output: Subject | None = None
    account_stamps: dict[FirebaseUid, str]
    created_at: UtcTimestamp
    available_at: UtcTimestamp
    dispatch_after: UtcTimestamp
    dispatch_generation: Counter = 0
    dispatch_pending: bool = False
    status: Status = "pending"
    attempts: Counter = 0
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=20)] = 5
    lease_generation: Counter = 0
    lease_expires_at: UtcTimestamp | None = None
    checkpoints: tuple[Checkpoint, ...] = ()
    result: JsonValue = None
    reason: Reason | None = None

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if (
            self.id != job_id(self.tenant_id, self.kind, self.operation_id)
            or self.grant.ref.tenant_id != self.tenant_id
            or self.grant.context.tenant_id != self.tenant_id
            or self.grant.action not in Action
            or (self.status == "running") != (self.lease_expires_at is not None)
            or self.attempts > self.max_attempts
            or len(self.checkpoints) > 100
            or len({item.key for item in self.checkpoints}) != len(self.checkpoints)
        ):
            raise ValueError("Invalid job bindings")
        return self


class Lease(Record):
    tenant_id: ResourceId
    job_id: ResourceId
    generation: Annotated[int, Field(strict=True, ge=1)]


class JobError(Exception):
    def __init__(self, code: str = "job_unavailable") -> None:
        super().__init__(code)


class LeaseLost(JobError):
    def __init__(self) -> None:
        super().__init__("job_lease_lost")


class AuthorizationUnavailable(JobError):
    def __init__(self) -> None:
        super().__init__("job_authorization_unavailable")


class RetryableError(JobError):
    """Only raise when the handler knows that no external side effect is outstanding."""


class OutcomeUnknown(JobError):
    def __init__(self) -> None:
        super().__init__("job_outcome_unknown")

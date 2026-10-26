"""Canonical one-shot schedules and a generation fence for delayed delivery events."""

from collections.abc import Callable
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    IanaTimezone,
    ResourceId,
    Scope,
    UtcTimestamp,
    Version,
    Visibility,
)
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.transactions import Transaction
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import ResourceKind, ResourceRef, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from brainoid_policy.transactions import transaction_policy
from pydantic import Field, TypeAdapter, ValidationError, model_validator

from brainoid_memory.direct_resources import DirectRecord, DirectResources
from brainoid_memory.models import MemoryValue
from brainoid_memory.notes import Title
from brainoid_memory.repository import FactTransaction
from brainoid_memory.tasks import Tasks

ReminderStatus = Literal["scheduled", "dispatched", "acknowledged", "cancelled", "expired"]


class DeliveryTarget(MemoryValue):
    kind: Literal["user", "device"]
    id: Annotated[str, Field(strict=True, max_length=128)]


class ReminderCreate(Scope):
    title: Title
    due_at: UtcTimestamp
    timezone: IanaTimezone
    delivery_target: DeliveryTarget
    task_id: ResourceId | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def optional_nonnull(self) -> Self:
        if "task_id" in self.model_fields_set and self.task_id is None:
            raise ValueError("Omit absent task_id")
        return self


class ReminderPatch(MemoryValue):
    title: Title | None = None
    due_at: UtcTimestamp | None = None
    timezone: IanaTimezone | None = None
    status: Literal["cancelled"] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("Nonempty changes without null required")
        return self


class ReminderReplan(ReminderPatch):
    """Internal typed mutation; never accepted as a public PATCH."""

    due_at: UtcTimestamp
    timezone: IanaTimezone
    snooze: Literal[True] = True


class ReminderSnooze(MemoryValue):
    due_at: UtcTimestamp
    timezone: IanaTimezone
    expected_version: Version

    def changes(self) -> ReminderReplan:
        return ReminderReplan(due_at=self.due_at, timezone=self.timezone, snooze=True)


class Reminder(DirectRecord):
    title: Title
    due_at: UtcTimestamp
    timezone: IanaTimezone
    delivery_target: DeliveryTarget
    task_id: ResourceId | None = None
    status: ReminderStatus = "scheduled"
    schedule_version: Version = 1


class ReminderView(ReminderCreate):
    id: ResourceId
    tenant_id: ResourceId
    version: Version
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    status: ReminderStatus
    schedule_version: Version
    owner_user_id: FirebaseUid | None = Field(default=None, exclude_if=lambda v: v is None)


class ReminderPage(MemoryValue):
    items: Annotated[list[ReminderView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


def view(record: Reminder) -> ReminderView:
    return ReminderView.model_validate(
        {k: getattr(record, k) for k in ReminderView.model_fields if getattr(record, k) is not None}
    )


class ScheduleReference(MemoryValue):
    """Content-free identity carried by future jobs, notifications and acknowledgements."""

    tenant_id: ResourceId
    reminder_id: ResourceId
    schedule_version: Version


class ScheduleIntent(MemoryValue):
    """Immutable acceptance evidence for this generation, never a bearer grant."""

    reference: ScheduleReference
    user_id: FirebaseUid
    accepted_version: Version
    authorization_stamp: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    account_stamp: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def schedule_intent_path(reference: ScheduleReference) -> str:
    identity = uuid5(UUID(reference.reminder_id), f"schedule:{reference.schedule_version}")
    return f"tenants/{reference.tenant_id}/reminder_schedules/{identity}"


def schedule_subject(tx: Transaction, policy: Evaluator, record: Reminder) -> UserSubject:
    reference = ScheduleReference(
        tenant_id=record.tenant_id, reminder_id=record.id, schedule_version=record.schedule_version
    )
    try:
        intent = ScheduleIntent.model_validate(tx.get(schedule_intent_path(reference)))
    except ValidationError:
        raise PolicyDenied() from None
    subject = UserSubject(user_id=intent.user_id)
    if (
        intent.reference != reference
        or intent.accepted_version > record.version
        or (record.owner_user_id is not None and intent.user_id != record.owner_user_id)
        or policy.principal(record.tenant_id, subject).stamp != intent.authorization_stamp
    ):
        raise PolicyDenied()
    try:
        if account_stamp(tx, intent.user_id) != intent.account_stamp:
            raise PolicyDenied()
    except AccountUnavailable:
        raise PolicyDenied() from None
    return subject


def current_schedule(record: Reminder, reference: ScheduleReference) -> bool:
    """Common fence; consumers must ALSO authorize current audience and their own transition."""
    return (
        not record.deleted
        and not record.deletion_epoch
        and record.tenant_id == reference.tenant_id
        and record.id == reference.reminder_id
        and record.schedule_version == reference.schedule_version
        and record.status not in {"cancelled", "expired"}
    )


class Reminders(DirectResources[ReminderCreate, ReminderPatch, Reminder]):
    kind = ResourceKind.REMINDER
    create_type = ReminderCreate
    patch_type = ReminderPatch
    record_type = Reminder

    @classmethod
    def linked(cls, access: FactTransaction, record: ReminderCreate | Reminder) -> None:
        tenant = access.principal.context.tenant_id
        policy = access._authority.policy
        cls.authorize_links(
            access._tx, policy, tenant, access.caller.subject, record, access.principal.user_id
        )
        if record.task_id is not None:
            Tasks.get(access, record.task_id)
        access.finish()

    @staticmethod
    def authorize_links(
        tx: Transaction,
        policy: Evaluator,
        tenant: ResourceId,
        subject: Subject,
        record: ReminderCreate | Reminder,
        owner: FirebaseUid | None,
    ) -> None:
        """Shared current dependency checks, including durable schedule consumers."""
        target = record.delivery_target
        if target.kind == "user":
            try:
                uid = TypeAdapter(FirebaseUid).validate_python(target.id)
                policy._member(tenant, uid)
                account_stamp(tx, uid)
            except (ValueError, AccountUnavailable):
                raise PolicyDenied() from None
            owner = record.owner_user_id if isinstance(record, Reminder) else owner
            if record.visibility == Visibility.PRIVATE and target.id != owner:
                raise PolicyDenied()
        else:
            try:
                identity = TypeAdapter(ResourceId).validate_python(target.id)
            except ValueError:
                raise PolicyDenied() from None
            device = policy._state.device(tenant, identity)
            if (
                device is None
                or device.tenant_id != tenant
                or device.id != identity
                or device.status != "active"
            ):
                raise PolicyDenied()
            # Admission never grants a future private session. The dispatcher must
            # require the owner's current reminders session before any content output.
            if (
                record.visibility == Visibility.PRIVATE
                and record.display_policy != DisplayPolicy.AUTHENTICATED_SESSION
            ):
                raise PolicyDenied()
        if record.task_id is not None:
            task = policy.resource(
                tenant,
                subject,
                ResourceRef(tenant_id=tenant, kind=ResourceKind.TASK, id=record.task_id),
            )
            if task.resource.deletion_epoch:
                raise PolicyDenied()
            if (
                task.scope.visibility == Visibility.PRIVATE
                and record.visibility != Visibility.PRIVATE
            ):
                raise PolicyDenied()

    @classmethod
    def validate_body(cls, access: FactTransaction, body: ReminderCreate | ReminderPatch) -> None:
        if isinstance(body, ReminderCreate):
            cls.linked(access, body)

    @classmethod
    def get(
        cls,
        access: FactTransaction,
        identity: ResourceId,
        *,
        write: bool = False,
        for_model: bool = False,
    ) -> tuple[Reminder, ResourceGrant]:
        record, grant = super().get(access, identity, write=write, for_model=for_model)
        cls.linked(access, record)
        return record, grant

    def patched(self, old: Reminder, body: ReminderPatch) -> Reminder:
        replan = isinstance(body, ReminderReplan)
        changed_time = (body.due_at is not None and body.due_at != old.due_at) or (
            body.timezone is not None and body.timezone != old.timezone
        )
        cancel = body.status == "cancelled" and old.status != "cancelled"
        if (replan or changed_time) and (body.due_at or old.due_at) <= self.now():
            raise APIError(ErrorCode.INVALID_REQUEST)
        return Reminder.model_validate(
            {
                **old.model_dump(),
                **body.model_dump(exclude_unset=True, exclude={"snooze"}),
                "schedule_version": old.schedule_version + int(replan or changed_time or cancel),
                "status": "cancelled"
                if body.status == "cancelled"
                else "scheduled"
                if replan or changed_time
                else old.status,
            }
        )

    def prepare_write(
        self, tx: Transaction, old: Reminder | None, record: Reminder
    ) -> Callable[[], None]:
        if old is None and record.due_at <= self.now():
            raise APIError(ErrorCode.INVALID_REQUEST)
        if old is not None and old.schedule_version == record.schedule_version:
            return lambda: None
        reference = ScheduleReference(
            tenant_id=record.tenant_id,
            reminder_id=record.id,
            schedule_version=record.schedule_version,
        )
        path = schedule_intent_path(reference)
        if tx.get(path) is not None:
            raise PolicyDenied()
        # DirectResources already authenticated/authorized this accepted mutation
        # with the independent ledger in the same transaction. Snapshot its durable
        # user authority, not the HTTP token or ephemeral Pi session lifetime.
        principal = transaction_policy(tx, now=self.now).principal(
            record.tenant_id, UserSubject(user_id=record.updated_by)
        )
        intent = ScheduleIntent(
            reference=reference,
            user_id=record.updated_by,
            accepted_version=record.version,
            authorization_stamp=principal.stamp,
            account_stamp=account_stamp(tx, record.updated_by),
        )
        return lambda: tx.create(path, intent.model_dump(mode="json"))


reminder_path = Reminders.path

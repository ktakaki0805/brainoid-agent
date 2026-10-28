"""Content-free notification generations and their current, recipient-only projection.

The immutable source (version 1) is separate from delivery receipts. A reminder's
record version can change without changing its schedule. No title is copied here.
"""

from datetime import timedelta
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, ResourceId, Scope, UtcTimestamp, Version, Visibility
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.device_snapshot import NotificationView as NotificationView
from brainoid_contracts.transactions import Document, Transaction
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import DeviceSubject, ResourceKind, ResourceRef, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied
from pydantic import Field, TypeAdapter, model_validator

from brainoid_memory.models import MemoryValue, StoredMetadata
from brainoid_memory.reminders import (
    DeliveryTarget,
    Reminder,
    Reminders,
    ScheduleReference,
    current_schedule,
    reminder_path,
    schedule_subject,
)
from brainoid_memory.repository import FactTransaction

VOICE_WINDOW = timedelta(minutes=5)
KIND = "dispatch_notification"


def notification_id(reference: ScheduleReference) -> ResourceId:
    return ResourceId(
        str(uuid5(UUID(reference.reminder_id), f"notification:{reference.schedule_version}"))
    )


def notification_path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/notifications/{identity}"


class Notification(Scope, StoredMetadata):
    reference: ScheduleReference
    target: DeliveryTarget
    # Receipt owner for lifecycle and authenticated inbox access; never a DB key.
    user_id: FirebaseUid
    due_at: UtcTimestamp
    scheduled_by: FirebaseUid
    recipient_stamp: str | None = None
    recipient_account_stamp: str | None = None

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if (
            self.id != notification_id(self.reference)
            or self.tenant_id != self.reference.tenant_id
            or self.version != 1
            or (self.visibility == Visibility.PRIVATE) != (self.owner_user_id is not None)
            or (self.target.kind == "user" and self.target.id != self.user_id)
        ):
            raise ValueError("Invalid notification binding")
        return self


class DeliveryState(MemoryValue):
    notification_id: ResourceId
    device_sequence: Version | None = None
    target: DeliveryTarget
    provider: Literal["pending", "accepted", "offline"] = "pending"
    accepted_at: UtcTimestamp | None = None
    displayed_at: UtcTimestamp | None = None
    acknowledged_at: UtcTimestamp | None = None
    updated_at: UtcTimestamp
    # Public projection metadata is independent of the immutable source/grant.
    # Missing legacy watermarks cannot establish an initial public version.
    projection_version: Version | None = None
    reminder_version: Version | None = None


def public_version(delivery: DeliveryState, reminder: Reminder) -> Version:
    """Count canonical edits since the last receipt without copying the title.

    A receipt update advances this value once and includes any simultaneous
    canonical status change in its new reminder watermark.
    """
    if (
        delivery.projection_version is None
        or delivery.reminder_version is None
        or delivery.reminder_version > reminder.version
    ):
        raise PolicyDenied()
    return delivery.projection_version + reminder.version - delivery.reminder_version


class NotificationPage(MemoryValue):
    items: Annotated[list[NotificationView], Field(max_length=100)]
    next_cursor: Annotated[str, Field(max_length=2048)] | None


class NotificationAck(MemoryValue):
    action: Literal["displayed", "acknowledged"]


def recipient_current(tx: Transaction, policy: Evaluator, record: Notification) -> None:
    if record.target.kind == "user":
        principal = policy.principal(record.tenant_id, UserSubject(user_id=record.user_id))
        try:
            stamp = account_stamp(tx, record.user_id)
        except AccountUnavailable:
            raise PolicyDenied() from None
        if principal.stamp != record.recipient_stamp or stamp != record.recipient_account_stamp:
            raise PolicyDenied()


def delivery_path(record: Notification) -> str:
    return (
        notification_path(record.tenant_id, record.id)
        + "/deliveries/"
        + fingerprint(record.target.model_dump(mode="json"))
    )


def read_notification(tx: Transaction, tenant: ResourceId, identity: ResourceId) -> Notification:
    document = tx.get(notification_path(tenant, identity))
    if document is None:
        raise PolicyDenied()
    record = Notification.model_validate(
        fenced_document(tx, tenant, "notifications", identity, document)
    )
    if (
        record.tenant_id != tenant
        or record.id != identity
        or record.deleted
        or record.deletion_epoch
    ):
        raise PolicyDenied()
    return record


def read_delivery(tx: Transaction, record: Notification) -> DeliveryState:
    result = DeliveryState.model_validate(tx.get(delivery_path(record)))
    if (
        result.notification_id != record.id
        or result.target != record.target
        or result.projection_version is None
        or result.reminder_version is None
    ):
        raise PolicyDenied()
    return result


def current_reminder(
    tx: Transaction, policy: Evaluator, subject: Subject, reference: ScheduleReference
) -> Reminder:
    tenant, identity = reference.tenant_id, reference.reminder_id
    grant = policy.resource(
        tenant, subject, ResourceRef(tenant_id=tenant, kind=ResourceKind.REMINDER, id=identity)
    )
    document = tx.get(reminder_path(tenant, identity))
    if document is None:
        raise PolicyDenied()
    record = Reminder.model_validate(fenced_document(tx, tenant, "reminders", identity, document))
    if not current_schedule(record, reference):
        raise PolicyDenied()
    if grant.principal.user_id is not None:
        try:
            account_stamp(tx, grant.principal.user_id)
        except AccountUnavailable:
            raise PolicyDenied() from None
    Reminders.authorize_links(tx, policy, tenant, subject, record, grant.principal.user_id)
    return record


def destination(tx: Transaction, policy: Evaluator, record: Notification) -> Subject:
    """Resolve TODAY's output identity; no original token/session is prolonged."""
    if record.target.kind == "user":
        return UserSubject(user_id=record.user_id)
    identity = TypeAdapter(ResourceId).validate_python(record.target.id)
    device = policy._state.device(record.tenant_id, identity)
    if (
        device is None
        or device.status != "active"
        or not device.capabilities & {"display", "audio_output"}
    ):
        raise PolicyDenied()
    session = None
    if record.visibility == Visibility.PRIVATE:
        slot = tx.get(f"tenants/{record.tenant_id}/private_session_slots/{identity}")
        if slot is None:
            raise PolicyDenied()
        session = TypeAdapter(ResourceId).validate_python(slot.get("session_id"))
    return DeviceSubject(
        device_id=identity,
        connection_generation=device.connection_generation,
        private_session_id=session,
    )


def authorize_delivery(
    tx: Transaction, policy: Evaluator, record: Notification
) -> tuple[Reminder, Subject]:
    """Both durable initiator and current destination must read the same schedule."""
    recipient_current(tx, policy, record)
    initiator = UserSubject(user_id=record.scheduled_by)
    reminder = current_reminder(tx, policy, initiator, record.reference)
    if (
        reminder.delivery_target != record.target
        or reminder.due_at != record.due_at
        or reminder.visibility != record.visibility
        or reminder.display_policy != record.display_policy
        or reminder.owner_user_id != record.owner_user_id
        or reminder.created_by != record.created_by
        or schedule_subject(tx, policy, reminder) != initiator
    ):
        raise PolicyDenied()
    output = destination(tx, policy, record)
    # Deliberately authorize a shared user recipient independently: sending to an
    # explicitly selected member isn't a generic cross-user output delegation.
    current_reminder(tx, policy, output, record.reference)
    if record.visibility == Visibility.PRIVATE:
        if policy.principal(record.tenant_id, output).user_id != record.owner_user_id:
            raise PolicyDenied()
    return reminder, output


def public_notification(access: FactTransaction, identity: ResourceId) -> Document:
    """API/export boundary, checking current schedule, admission and recipient stamps."""
    tx, tenant = access._tx, access.principal.context.tenant_id
    record = read_notification(tx, tenant, identity)
    policy, caller = access._authority.policy, access.caller.subject
    reminder = current_reminder(tx, policy, caller, record.reference)
    recipient_current(tx, policy, record)
    if schedule_subject(tx, policy, reminder) != UserSubject(user_id=record.scheduled_by):
        raise PolicyDenied()
    if isinstance(caller, UserSubject):
        if caller.user_id != record.user_id:
            raise PolicyDenied()
    elif caller != destination(tx, policy, record):
        raise PolicyDenied()
    delivery = read_delivery(tx, record)
    access.finish()
    return {
        "id": record.id,
        "tenant_id": tenant,
        "version": public_version(delivery, reminder),
        "created_at": record.created_at.isoformat(),
        "updated_at": max(delivery.updated_at, reminder.updated_at).isoformat(),
        "reminder_id": reminder.id,
        "title": reminder.title,
        "status": "acknowledged"
        if delivery.acknowledged_at
        else "displayed"
        if delivery.displayed_at
        else "sent"
        if delivery.accepted_at
        else "pending",
        "target_kind": record.target.kind,
        "target_id": record.target.id,
        "expires_at": (record.due_at + VOICE_WINDOW).isoformat(),
        "visibility": record.visibility,
        "display_policy": record.display_policy,
    }


def acknowledge(
    access: FactTransaction, identity: ResourceId, action: Literal["displayed", "acknowledged"]
) -> None:
    """Authenticated internal receipt path; old IDs can never acknowledge a new generation."""
    if action not in {"displayed", "acknowledged"}:
        raise ValueError("Invalid notification receipt")
    public_notification(access, identity)
    tx, tenant = access._tx, access.principal.context.tenant_id
    record = read_notification(tx, tenant, identity)
    delivery = read_delivery(tx, record)
    # A device display acknowledgement is not evidence that a person confirmed it.
    if action == "acknowledged" and not isinstance(access.caller.subject, UserSubject):
        raise PolicyDenied()
    now = access._repo._now()
    field = "displayed_at" if action == "displayed" else "acknowledged_at"
    reminder = current_reminder(
        tx, access._authority.policy, access.caller.subject, record.reference
    )
    if getattr(delivery, field) is None:
        confirm_reminder = action == "acknowledged" and reminder.status != "acknowledged"
        updated = delivery.model_copy(
            update={
                field: now,
                "updated_at": max(now, delivery.updated_at, reminder.updated_at),
                "projection_version": public_version(delivery, reminder) + 1,
                "reminder_version": reminder.version + int(confirm_reminder),
            }
        )
        access.finish()
        tx.set(delivery_path(record), updated.model_dump(mode="json"))
        if confirm_reminder:
            tx.set(
                reminder_path(tenant, reminder.id),
                reminder.model_copy(
                    update={
                        "status": "acknowledged",
                        "version": reminder.version + 1,
                        "updated_at": now,
                    }
                ).model_dump(mode="json"),
            )

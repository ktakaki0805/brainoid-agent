"""Local scheduler boundary and notification handler over the shared durable Jobs.

The provider port carries identifiers and an expiry only. It must deduplicate the
stable effect key and enforce expiry at playback; actual content requires a fresh
authenticated read. APP-05/DEV-04 supply real transports in later tickets.
"""

from dataclasses import dataclass, replace
from functools import partial
from typing import Literal, Protocol
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp, normalize_utc
from brainoid_contracts.deletion import fenced_document
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost
from brainoid_jobs.models import Checkpoint, SourceObsolete, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_memory.notifications import (
    KIND,
    VOICE_WINDOW,
    DeliveryState,
    Notification,
    authorize_delivery,
    current_reminder,
    delivery_path,
    notification_id,
    notification_path,
    public_version,
    read_delivery,
    read_notification,
)
from brainoid_memory.reminders import Reminder, ScheduleReference, reminder_path, schedule_subject
from brainoid_policy.accounts import account_stamp
from brainoid_policy.models import (
    Action,
    DeviceSubject,
    ResourceKind,
    ResourceRef,
    Subject,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_policy.transactions import resource_state
from pydantic import JsonValue, TypeAdapter, ValidationError

from brainoid_worker.dispatcher import JobPages
from brainoid_worker.entry import Execution

STEP = "deliver"
Receipt = Literal["accepted", "offline"]
RECEIPT = TypeAdapter(Receipt)


class NotificationProvider(Protocol):
    def send(self, key: str, payload: dict[str, JsonValue]) -> Receipt:
        """Idempotent acceptance, not a device display or a person's acknowledgement."""
        ...

    def lookup(self, key: str) -> Receipt | None:
        """Reconcile only. None means unresolved; never resend an unknown effect."""
        ...


@dataclass(frozen=True)
class ScanPage:
    inspected: int
    created: int
    next_cursor: str | None
    busy: bool = False


class DueReminders:
    """Bounded ID-ordered due/status scan with a durable cursor and tenant lease.

    Each pass reads every reminder, including historical ones. Limit bounds one
    page, not the full tenant's scan cost. Candidate fields are never authority.
    """

    def __init__(self, jobs: Jobs, pages: JobPages) -> None:
        self.jobs, self.pages = jobs, pages

    @staticmethod
    def path(tenant: ResourceId) -> str:
        return f"tenants/{tenant}/notification_scans/due"

    def scan(self, tenant: ResourceId, *, limit: int = 100) -> ScanPage:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid scan limit")
        token = str(uuid4())

        def acquire(tx: Transaction) -> tuple[bool, str | None]:
            state = tx.get(self.path(tenant)) or {}
            tenant_state = self.jobs._policy(tx)._state.tenant(tenant)
            if tenant_state is None or tenant_state.status != "active":
                raise PolicyDenied()
            now = normalize_utc(self.jobs.now())
            if (
                state.get("lease")
                and TypeAdapter(UtcTimestamp).validate_python(state["expires_at"]) > now
            ):
                return False, None
            cursor = state.get("cursor")
            if cursor is not None and (
                not isinstance(cursor, str) or "/" in cursor or len(cursor) > 1500
            ):
                raise ValueError("Invalid durable scan cursor")
            tx.set(
                self.path(tenant),
                {
                    "cursor": cursor,
                    "lease": token,
                    "expires_at": (now + self.jobs.lease_duration).isoformat(),
                },
            )
            return True, cursor

        acquired, cursor = self.jobs.store.run(acquire)
        if not acquired:
            return ScanPage(0, 0, None, True)
        collection = f"tenants/{tenant}/reminders"
        documents = self.pages.page(collection, after=cursor, limit=limit)
        created = 0
        for path, candidate in documents:
            if path.rsplit("/", 1)[0] != collection:
                raise ValueError("Invalid scan path")
            try:
                identity = TypeAdapter(ResourceId).validate_python(path.rsplit("/", 1)[-1])
                if candidate.get("status") != "scheduled":
                    continue
                created += self.jobs.store.run(
                    partial(self._claim, tenant=tenant, identity=identity, token=token)
                )
            except PolicyUnavailable:
                raise
            except (PolicyDenied, ValidationError):
                continue
        next_cursor = documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None

        def finish(tx: Transaction) -> None:
            self._lease(tx, tenant, token)
            tx.set(self.path(tenant), {"cursor": next_cursor, "lease": None})

        self.jobs.store.run(finish)
        return ScanPage(len(documents), created, next_cursor)

    def _lease(self, tx: Transaction, tenant: ResourceId, token: str) -> None:
        state = tx.get(self.path(tenant))
        if (
            state is None
            or state.get("lease") != token
            or TypeAdapter(UtcTimestamp).validate_python(state.get("expires_at")) <= self.jobs.now()
        ):
            raise LeaseLost()

    def _claim(self, tx: Transaction, tenant: ResourceId, identity: ResourceId, token: str) -> int:
        self._lease(tx, tenant, token)
        document = tx.get(reminder_path(tenant, identity))
        if document is None:
            return 0
        record = Reminder.model_validate(document)
        now = normalize_utc(self.jobs.now())
        if record.status != "scheduled" or record.due_at > now:
            return 0
        reference = ScheduleReference(
            tenant_id=tenant, reminder_id=identity, schedule_version=record.schedule_version
        )
        policy = self.jobs._policy(tx)
        subject = schedule_subject(tx, policy, record)
        record = current_reminder(tx, policy, subject, reference)
        notification = Notification(
            id=notification_id(reference),
            tenant_id=tenant,
            reference=reference,
            target=record.delivery_target,
            user_id=TypeAdapter(FirebaseUid).validate_python(record.delivery_target.id)
            if record.delivery_target.kind == "user"
            else subject.user_id,
            due_at=record.due_at,
            scheduled_by=subject.user_id,
            recipient_stamp=policy.principal(
                tenant,
                UserSubject(
                    user_id=TypeAdapter(FirebaseUid).validate_python(record.delivery_target.id)
                ),
            ).stamp
            if record.delivery_target.kind == "user"
            else None,
            recipient_account_stamp=account_stamp(
                tx, TypeAdapter(FirebaseUid).validate_python(record.delivery_target.id)
            )
            if record.delivery_target.kind == "user"
            else None,
            visibility=record.visibility,
            display_policy=record.display_policy,
            owner_user_id=record.owner_user_id,
            created_by=record.created_by,
            version=1,
            deletion_epoch=0,
            schema_version=1,
            created_at=now,
            updated_at=now,
            deleted=False,
            suppress_reuse=False,
        )
        source_path = notification_path(tenant, notification.id)
        identity_job = job_id(tenant, KIND, notification.id)
        existing = tx.get(source_path)
        saved_job = tx.get(job_path(tenant, identity_job))
        if fenced_document(
            tx, tenant, "notifications", notification.id, notification.model_dump(mode="json")
        )["deleted"]:
            raise PolicyDenied()
        if existing is not None:
            saved = Notification.model_validate(existing)
            if saved.reference != reference or saved.target != notification.target:
                raise ValueError("Notification conflict")
            # Never recreate an erased receipt/job from a stale reminder snapshot.
            return 0
        if saved_job is not None or tx.get(delivery_path(notification)) is not None:
            raise ValueError("Orphan notification state")
        # A new source cannot be authorized by add_pending's existing-source rule.
        # Derive a narrow immutable READ grant from this transaction's authorized
        # canonical reminder and validate its identical scope/principal explicitly.
        grant = policy.resource(
            tenant, subject, ResourceRef(tenant_id=tenant, kind=ResourceKind.REMINDER, id=record.id)
        )
        state = resource_state(
            ResourceRef(tenant_id=tenant, kind=ResourceKind.NOTIFICATION, id=notification.id),
            notification.model_dump(mode="json"),
        )
        assert state is not None
        grant = replace(grant, resource=state, action=Action.READ)
        stamps = self.jobs._accounts(tx, grant, None)
        job = Job(
            id=identity_job,
            tenant_id=tenant,
            operation_id=notification.id,
            kind=KIND,
            grant=grant,
            account_stamps=stamps,
            created_at=now,
            available_at=now,
            dispatch_after=now,
        )
        delivery = DeliveryState(
            notification_id=notification.id,
            target=notification.target,
            updated_at=now,
            projection_version=1,
            reminder_version=record.version,
        )
        self._lease(tx, tenant, token)
        tx.create(source_path, notification.model_dump(mode="json"))
        tx.create(delivery_path(notification), delivery.model_dump(mode="json"))
        tx.create(job_path(tenant, job.id), job.model_dump(mode="json"))
        return 1


class DispatchNotification:
    def __init__(self, jobs: Jobs, provider: NotificationProvider) -> None:
        self.jobs, self.provider = jobs, provider

    def _current(
        self, tx: Transaction, job: Job
    ) -> tuple[Notification, DeliveryState, Reminder, Subject]:
        if (
            job.kind != KIND
            or job.grant.ref.kind != ResourceKind.NOTIFICATION
            or job.grant.ref.id != job.operation_id
        ):
            raise PolicyDenied()
        record = read_notification(tx, job.tenant_id, job.operation_id)
        if job.grant.principal.subject != UserSubject(user_id=record.scheduled_by):
            raise PolicyDenied()
        reminder, output = authorize_delivery(tx, self.jobs._policy(tx), record)
        if record.due_at > self.jobs.now():
            raise PolicyDenied()
        delivery = read_delivery(tx, record)
        public_version(delivery, reminder)
        return record, delivery, reminder, output

    def run(self, execution: Execution) -> JsonValue:
        try:
            return self._run(execution)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except PolicyDenied:
            raise SourceObsolete() from None

    def _run(self, execution: Execution) -> JsonValue:
        def preflight(tx: Transaction) -> dict[str, JsonValue] | None:
            job = self.jobs._live(tx, execution.lease)
            if job is None:
                raise LeaseLost()
            record, delivery, _, output = self._current(tx, job)
            if delivery.acknowledged_at is not None:
                return None
            expires = record.due_at + VOICE_WINDOW
            payload: dict[str, JsonValue] = {
                "tenant_id": record.tenant_id,
                "job_lease_generation": execution.lease.generation,
                "notification_id": record.id,
                "target_kind": record.target.kind,
                "target_id": record.target.id,
                "expires_at": expires.isoformat(),
                "auto_play": self.jobs.now() <= expires,
            }
            if isinstance(output, DeviceSubject):
                device = self.jobs._policy(tx)._state.device(job.tenant_id, output.device_id)
                assert device is not None
                payload["auto_play"] = bool(
                    payload["auto_play"] and "audio_output" in device.capabilities
                )
                payload["connection_generation"] = output.connection_generation
                if output.private_session_id is not None:
                    payload["private_session_id"] = output.private_session_id
            return payload

        if self.jobs.store.run(preflight) is None:
            return None

        def invoke(key: str) -> JsonValue:
            # Recheck after the durable start marker and immediately before I/O.
            payload = self.jobs.store.run(preflight)
            if payload is None:
                return None
            return RECEIPT.validate_python(self.provider.send(key, payload))

        return execution.effect(STEP, invoke)

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        try:
            self._commit(tx, job, result)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except PolicyDenied:
            raise SourceObsolete() from None

    def _commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        record, delivery, reminder, _ = self._current(tx, job)
        if result is None and delivery.acknowledged_at is not None:
            return
        receipt = RECEIPT.validate_python(result)
        now = normalize_utc(self.jobs.now())
        dispatch_reminder = receipt == "accepted" and reminder.status == "scheduled"
        updated = delivery.model_copy(
            update={
                "provider": receipt,
                "projection_version": public_version(delivery, reminder) + 1,
                "reminder_version": reminder.version + int(dispatch_reminder),
                "updated_at": max(now, delivery.updated_at, reminder.updated_at),
                "accepted_at": delivery.accepted_at or (now if receipt == "accepted" else None),
            }
        )
        tx.set(delivery_path(record), updated.model_dump(mode="json"))
        if dispatch_reminder:
            tx.set(
                reminder_path(job.tenant_id, reminder.id),
                reminder.model_copy(
                    update={
                        "status": "dispatched",
                        "version": reminder.version + 1,
                        "updated_at": now,
                    }
                ).model_dump(mode="json"),
            )

    def reconcile_unknown(self, tenant: ResourceId, identity: ResourceId) -> bool:
        """Query a stable provider key; repair only the SAME failed grant/generation.

        Unknown or absent provider evidence stays failed. No send is performed.
        A restored completed checkpoint is processed by the normal worker again.
        """
        job = self.jobs.get(tenant, identity)
        if (
            job is None
            or job.kind != KIND
            or job.status != "failed"
            or job.reason != "outcome_unknown"
        ):
            return False
        result = self.provider.lookup(f"{job.id}:{STEP}")
        if result is None:
            return False
        receipt = RECEIPT.validate_python(result)

        def repair(tx: Transaction) -> bool:
            current = self.jobs._read(tx, tenant, identity)
            if current != job:
                return False
            if self.jobs._invalid_reason(tx, current) is not None:
                return False
            self._current(tx, current)
            if not any(c.key == STEP and c.state == "started" for c in current.checkpoints):
                return False
            now = normalize_utc(self.jobs.now())
            self.jobs._write(
                tx,
                current,
                status="pending",
                reason=None,
                attempts=max(0, current.attempts - 1),
                available_at=now,
                dispatch_after=now,
                dispatch_pending=False,
                checkpoints=tuple(
                    Checkpoint(key=STEP, state="completed", result=receipt) if c.key == STEP else c
                    for c in current.checkpoints
                ),
            )
            return True

        try:
            return self.jobs.store.run(repair)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except PolicyDenied:
            return False

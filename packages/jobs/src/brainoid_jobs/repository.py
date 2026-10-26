"""Transactional outbox, authorization revalidation and lease fencing.

All external effects belong outside these callbacks. ControlLedger performs the
same strongly consistent read-only fence as API authorization; never writes here.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from brainoid_contracts import FirebaseUid, ResourceId, normalize_utc
from brainoid_contracts.transactions import Document, Transaction, TransactionStore
from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import Action, ResourceKind, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable, ResourceGrant
from brainoid_policy.transactions import resource_state, transaction_policy
from pydantic import JsonValue, ValidationError

from brainoid_jobs.models import (
    TERMINAL,
    AuthorizationUnavailable,
    Checkpoint,
    Delivery,
    Job,
    JobError,
    Lease,
    LeaseLost,
    OutcomeUnknown,
    Reason,
    job_id,
)


def path(tenant: ResourceId, identity: ResourceId) -> str:
    return f"tenants/{tenant}/jobs/{identity}"


class Jobs:
    def __init__(
        self,
        store: TransactionStore,
        ledger: ControlLedger,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lease_duration: timedelta = timedelta(minutes=1),
        dispatch_timeout: timedelta = timedelta(minutes=2),
        retry_delay: timedelta = timedelta(seconds=5),
        allow_untracked_bootstrap: bool = False,
    ) -> None:
        if min(lease_duration, dispatch_timeout, retry_delay) <= timedelta(0):
            raise ValueError("Durations must be positive")
        self.store = store
        self.ledger = ledger
        self.now = now
        self.lease_duration = lease_duration
        self.dispatch_timeout = dispatch_timeout
        self.retry_delay = retry_delay
        self.allow_untracked = allow_untracked_bootstrap

    def _policy(self, tx: Transaction) -> Evaluator:
        return transaction_policy(
            tx,
            ledger=self.ledger,
            now=self.now,
            allow_untracked_bootstrap=self.allow_untracked,
        )

    def _accounts(
        self, tx: Transaction, grant: ResourceGrant, output: Subject | None
    ) -> dict[FirebaseUid, str]:
        users = {grant.principal.user_id} if grant.principal.user_id is not None else set()
        if isinstance(output, UserSubject):
            users.add(output.user_id)
        return {uid: account_stamp(tx, uid) for uid in users}

    def add_pending(
        self,
        tx: Transaction,
        *,
        operation_id: ResourceId,
        kind: str,
        grant: ResourceGrant,
        output: Subject | None = None,
        available_at: datetime | None = None,
        max_attempts: int = 5,
        source_replacement: Document | None = None,
    ) -> Job:
        """Call after business reads/authorization, BEFORE any transaction writes.

        The caller then writes business data through this same transaction. A saved
        source grant describes the canonical source version read in this transaction.
        For an atomic source update, pass source_replacement: WRITE is required and
        only version + 1 and business fields may change. This method writes that
        replacement and binds its new version, preserving all authorization metadata.
        New child resources require a later business-specific parent/child resolver.
        """
        tenant = grant.ref.tenant_id
        identity = job_id(tenant, kind, operation_id)
        existing = tx.get(path(tenant, identity))
        current = self._policy(tx).worker(grant, output=output)
        stamps = self._accounts(tx, current, output)
        if source_replacement is not None:
            updated = resource_state(current.ref, source_replacement)
            expected = current.resource.model_copy(update={"version": current.resource.version + 1})
            if current.action != Action.WRITE or updated != expected:
                raise JobError("invalid_source_replacement")
            current = replace(current, resource=updated)
        now = normalize_utc(self.now())
        job = Job(
            id=identity,
            tenant_id=tenant,
            operation_id=operation_id,
            kind=kind,
            grant=current,
            output=output,
            account_stamps=stamps,
            created_at=now,
            available_at=available_at or now,
            dispatch_after=available_at or now,
            max_attempts=max_attempts,
        )
        if existing is not None:
            saved = Job.model_validate(existing)
            if (
                saved.grant != current
                or saved.output != output
                or saved.kind != kind
                or saved.account_stamps != stamps
                or saved.max_attempts != max_attempts
                or (available_at is not None and saved.available_at != available_at)
            ):
                raise JobError("job_conflict")
            return saved
        if source_replacement is not None:
            tx.set(f"tenants/{tenant}/{current.ref.kind}/{current.ref.id}", source_replacement)
        tx.create(path(tenant, identity), job.model_dump(mode="json"))
        return job

    def get(self, tenant: ResourceId, identity: ResourceId) -> Job | None:
        return self.store.run(lambda tx: self._read(tx, tenant, identity))

    def add_created_memory(
        self,
        tx: Transaction,
        *,
        grant: ResourceGrant,
        source: Document,
        before_write: Callable[[], None],
    ) -> Job:
        """Atomic new memory and dormant indexing outbox; no synthetic parent grant.

        The memory adapter must first validate a complete canonical Fact, its current
        dependencies and revision fence in this transaction. This narrow creation
        path independently checks current principal/search policy and accounts, exact
        source metadata, version/epoch zero-state and actual DB nonexistence. It never
        overlays authority documents or relaxes add_pending's existing-source rules.

        MEM-05 converts this identifier-only event into a configuration-bound job.
        Its handler revalidates all canonical dependencies under an acquired lease
        before sending content to an embedding provider. This event stays waiting.
        """
        state = resource_state(grant.ref, source)
        if (
            grant.ref.kind != ResourceKind.MEMORY
            or grant.action != Action.MODEL_READ
            or state != grant.resource
            or state is None
            or state.version != 1
            or state.deletion_epoch != 0
            or source.get("deleted") is not False
            or source.get("suppress_reuse") is not False
            or source.get("status") != "confirmed"
            or source.get("origin") != "user_explicit"
            or source.get("created_by") != grant.principal.user_id
        ):
            raise JobError("invalid_source_creation")
        current = self._policy(tx).search(
            grant.ref.tenant_id, grant.principal.subject, ResourceKind.MEMORY
        )
        if (
            current.principal != grant.principal
            or current.principal.user_id is None
            or "memory.write" not in current.principal.context.actor.allowed_scopes
            or not any(item.matches(state) for item in current.filters)
        ):
            raise PolicyDenied()
        source_path = f"tenants/{grant.ref.tenant_id}/{grant.ref.kind}/{grant.ref.id}"
        identity = job_id(grant.ref.tenant_id, "memory_index", grant.ref.id)
        if (
            tx.get(source_path) is not None
            or tx.get(path(grant.ref.tenant_id, identity)) is not None
        ):
            raise JobError("source_already_exists")
        stamps = self._accounts(tx, grant, None)
        now = normalize_utc(self.now())
        job = Job(
            id=identity,
            tenant_id=grant.ref.tenant_id,
            operation_id=grant.ref.id,
            kind="memory_index",
            grant=grant,
            account_stamps=stamps,
            created_at=now,
            available_at=now,
            dispatch_after=now,
            status="waiting",
        )
        before_write()
        tx.create(source_path, source)
        tx.create(path(job.tenant_id, job.id), job.model_dump(mode="json"))
        return job

    def _read(self, tx: Transaction, tenant: ResourceId, identity: ResourceId) -> Job | None:
        document = tx.get(path(tenant, identity))
        if document is None or document.get("quarantined") is True:
            return None
        try:
            job = Job.model_validate(document)
            if job.tenant_id != tenant or job.id != identity:
                raise ValueError("Invalid job location")
            return job
        except (ValidationError, ValueError):
            # Keep a content-free terminal record; malformed grants/payloads are never executed.
            tx.set(
                path(tenant, identity),
                {
                    "id": identity,
                    "tenant_id": tenant,
                    "status": "failed",
                    "reason": "invalid_job",
                    "quarantined": True,
                },
            )
            return None

    def _write(self, tx: Transaction, job: Job, **updates: object) -> Job:
        updated = Job.model_validate({**job.model_dump(), **updates})
        tx.set(path(job.tenant_id, job.id), updated.model_dump(mode="json"))
        return updated

    def _stop(self, tx: Transaction, job: Job, status: str, reason: Reason) -> Job:
        return self._write(
            tx,
            job,
            status=status,
            reason=reason,
            lease_expires_at=None,
            dispatch_pending=False,
        )

    def _invalid_reason(self, tx: Transaction, job: Job) -> Reason | None:
        source = tx.get(f"tenants/{job.tenant_id}/{job.grant.ref.kind}/{job.grant.ref.id}")
        if source is None or source.get("deleted") is True:
            return "source_deleted"
        if (
            source.get("version") != job.grant.resource.version
            or source.get("deletion_epoch") != job.grant.resource.deletion_epoch
        ):
            return "source_changed"
        try:
            grant = self._policy(tx).worker(job.grant, output=job.output)
            if self._accounts(tx, grant, job.output) != job.account_stamps:
                return "account_changed"
        except AccountUnavailable:
            return "account_changed"
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except PolicyDenied:
            return "authorization_changed"
        return None

    def prepare_dispatch(
        self, tenant: ResourceId, identity: ResourceId, *, queue_name: str | None = None
    ) -> Delivery | None:
        def work(tx: Transaction) -> Delivery | None:
            job = self._read(tx, tenant, identity)
            now = normalize_utc(self.now())
            if (
                job is None
                or job.status in TERMINAL
                or job.status == "waiting"
                or job.available_at > now
                or (
                    queue_name is not None
                    and job.attributes.get("dispatch_queue", "default") != queue_name
                )
            ):
                return None
            if job.status == "running":
                if job.lease_expires_at is not None and job.lease_expires_at > now:
                    return None
                if any(c.state == "started" for c in job.checkpoints):
                    self._stop(tx, job, "failed", "outcome_unknown")
                    return None
                if job.attempts >= job.max_attempts:
                    self._stop(tx, job, "failed", "attempts_exhausted")
                    return None
                # Reclaimed work gets a fresh dispatch name even if Tasks retained the old name.
                job = job.model_copy(
                    update={
                        "status": "pending",
                        "lease_expires_at": None,
                        "dispatch_after": now,
                        "dispatch_pending": False,
                        "reason": "lease_expired",
                    }
                )
            if job.dispatch_after <= now:
                job = self._write(
                    tx,
                    job,
                    dispatch_generation=job.dispatch_generation + 1,
                    dispatch_pending=True,
                    dispatch_after=now + self.dispatch_timeout,
                )
            elif not job.dispatch_pending:
                return None
            return Delivery(
                tenant_id=tenant,
                job_id=identity,
                dispatch_generation=job.dispatch_generation,
            )

        return self.store.run(work)

    def dispatched(self, delivery: Delivery) -> None:
        def work(tx: Transaction) -> None:
            job = self._read(tx, delivery.tenant_id, delivery.job_id)
            if (
                job is not None
                and job.status == "pending"
                and job.dispatch_pending
                and job.dispatch_generation == delivery.dispatch_generation
            ):
                self._write(tx, job, dispatch_pending=False)

        self.store.run(work)

    def acquire(self, delivery: Delivery, *, known_handler: Callable[[str], bool]) -> Lease | None:
        def work(tx: Transaction) -> Lease | None:
            job = self._read(tx, delivery.tenant_id, delivery.job_id)
            now = normalize_utc(self.now())
            if (
                job is None
                or job.status in TERMINAL
                or job.available_at > now
                or delivery.dispatch_generation != job.dispatch_generation
                or job.status in ("running", "waiting")
            ):
                return None
            reason = self._invalid_reason(tx, job)
            if reason is not None:
                self._stop(tx, job, "obsolete", reason)
                return None
            if not known_handler(job.kind):
                self._stop(tx, job, "failed", "unknown_handler")
                return None
            if job.attempts >= job.max_attempts:
                self._stop(tx, job, "failed", "attempts_exhausted")
                return None
            job = self._write(
                tx,
                job,
                status="running",
                attempts=job.attempts + 1,
                lease_generation=job.lease_generation + 1,
                lease_expires_at=now + self.lease_duration,
                dispatch_pending=False,
                reason=None,
            )
            return Lease(tenant_id=job.tenant_id, job_id=job.id, generation=job.lease_generation)

        return self.store.run(work)

    def _live(self, tx: Transaction, lease: Lease) -> Job | None:
        job = self._read(tx, lease.tenant_id, lease.job_id)
        if (
            job is None
            or job.status != "running"
            or job.lease_generation != lease.generation
            or job.lease_expires_at is None
            or job.lease_expires_at <= normalize_utc(self.now())
        ):
            return None
        reason = self._invalid_reason(tx, job)
        if reason is not None:
            self._stop(tx, job, "obsolete", reason)
            return None
        return job

    def check(self, lease: Lease, *, renew: bool = False) -> Job:
        def work(tx: Transaction) -> Job | None:
            job = self._live(tx, lease)
            if job is not None and renew:
                return self._write(
                    tx,
                    job,
                    lease_expires_at=normalize_utc(self.now()) + self.lease_duration,
                )
            return job

        result = self.store.run(work)
        if result is None:
            raise LeaseLost()
        return result

    def start_effect(self, lease: Lease, key: str) -> Checkpoint:
        def work(tx: Transaction) -> Checkpoint | None:
            job = self._live(tx, lease)
            if job is None:
                return None
            existing = next((c for c in job.checkpoints if c.key == key), None)
            if existing is not None:
                if existing.state == "started":
                    raise OutcomeUnknown()
                return existing
            checkpoint = Checkpoint(key=key, state="started")
            self._write(tx, job, checkpoints=(*job.checkpoints, checkpoint))
            return checkpoint

        checkpoint = self.store.run(work)
        if checkpoint is None:
            raise LeaseLost()
        return checkpoint

    def effect(
        self,
        lease: Lease,
        key: str,
        invoke: Callable[[str], JsonValue],
        *,
        commit: Callable[[Transaction, Job, JsonValue], JsonValue] | None = None,
    ) -> JsonValue:
        # A start marker cannot authorize reexecution. The marker and callback may be
        # separated by a crash, so even a never-started call can require reconciliation.
        job = self.check(lease)
        existing = next((c for c in job.checkpoints if c.key == key), None)
        if existing is not None:
            if existing.state == "completed":
                return existing.result
            raise OutcomeUnknown()
        checkpoint = self.start_effect(lease, key)
        if checkpoint.state == "completed":
            return checkpoint.result
        result = invoke(f"{lease.job_id}:{key}")

        def work(tx: Transaction) -> tuple[bool, JsonValue]:
            current = self._live(tx, lease)
            if current is None:
                return False, None
            # Canonical validation + derived data + content-free receipt are atomic.
            # The raw provider result never enters the durable job envelope.
            saved = result if commit is None else commit(tx, current, result)
            self._write(
                tx,
                current,
                checkpoints=tuple(
                    c.model_copy(update={"state": "completed", "result": saved})
                    if c.key == key
                    else c
                    for c in current.checkpoints
                ),
            )
            return True, saved

        committed, saved = self.store.run(work)
        if not committed:
            raise LeaseLost()
        return saved

    def obsolete(self, lease: Lease) -> None:
        def work(tx: Transaction) -> None:
            job = self._read(tx, lease.tenant_id, lease.job_id)
            if (
                job is not None
                and job.status == "running"
                and job.lease_generation == lease.generation
            ):
                self._stop(tx, job, "obsolete", "source_changed")

        self.store.run(work)

    def complete(
        self,
        lease: Lease,
        result: JsonValue,
        commit: Callable[[Transaction, Job, JsonValue], None],
    ) -> bool:
        def work(tx: Transaction) -> bool:
            job = self._live(tx, lease)
            if job is None:
                return False
            # Business callbacks must do their own reads before writes and use this
            # transaction only. Retried callbacks must not call providers or queues.
            commit(tx, job, result)
            self._write(
                tx,
                job,
                status="succeeded",
                result=result,
                lease_expires_at=None,
                reason=None,
            )
            return True

        return self.store.run(work)

    def defer_unavailable(
        self,
        lease: Lease,
        *,
        reason: Literal["authorization_unavailable", "persistence_unavailable"],
    ) -> None:
        """Reconcile this generation after an infrastructure outage, without business writes.

        No current authorization is needed to stop execution. The same generation
        may be stopped even after its deadline, but a reclaimed/new lease is never
        modified. Read the durable state first: a lost commit response may hide a
        completed checkpoint or terminal outcome, which must never be overwritten.
        A successful deferral refunds the interrupted attempt. If DB access is still
        unavailable, leave the persisted lease/attempt for ordinary expiry recovery.
        An unresolved external call always requires outcome reconciliation.
        """

        def work(tx: Transaction) -> None:
            job = self._read(tx, lease.tenant_id, lease.job_id)
            if job is None or job.status != "running" or job.lease_generation != lease.generation:
                return
            if any(c.state == "started" for c in job.checkpoints):
                self._stop(tx, job, "failed", "outcome_unknown")
                return
            due = normalize_utc(self.now()) + self.retry_delay
            self._write(
                tx,
                job,
                status="pending",
                lease_expires_at=None,
                attempts=job.attempts - 1,
                available_at=due,
                dispatch_after=due,
                dispatch_pending=False,
                reason=reason,
            )

        self.store.run(work)

    def fail(self, lease: Lease, *, retry: bool = False) -> None:
        def work(tx: Transaction) -> None:
            job = self._live(tx, lease)
            if job is None:
                return
            if any(c.state == "started" for c in job.checkpoints):
                self._stop(tx, job, "failed", "outcome_unknown")
            elif retry and job.attempts < job.max_attempts:
                due = normalize_utc(self.now()) + self.retry_delay
                self._write(
                    tx,
                    job,
                    status="pending",
                    lease_expires_at=None,
                    available_at=due,
                    dispatch_after=due,
                    dispatch_pending=False,
                    reason="retry_requested",
                )
            else:
                reason: Reason = "attempts_exhausted" if retry else "handler_error"
                self._stop(tx, job, "failed", reason)

        self.store.run(work)

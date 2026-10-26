"""Deletion-only Jobs composition: acceptance survives requester revocation.

Never install this executor on an interactive worker. Its sole authority is an
immutable intent already committed with its exact fact tombstone and operation.
"""

from datetime import timedelta
from typing import Literal

from brainoid_contracts import ResourceId, normalize_utc
from brainoid_contracts.deletion import tombstone_path
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Delivery, Job, Jobs, Lease, LeaseLost
from brainoid_jobs.models import Reason
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionLedger
from brainoid_memory.forget import operation_path
from brainoid_policy.control_ledger import LedgerError
from pydantic import ValidationError

KIND = "memory_purge"


class PurgeJobs(Jobs):
    """Only register memory_purge on the dedicated authenticated worker/queue."""

    def __init__(self, jobs: Jobs, deletion_ledger: DeletionLedger) -> None:
        super().__init__(
            jobs.store,
            jobs.ledger,
            now=jobs.now,
            lease_duration=jobs.lease_duration,
            dispatch_timeout=jobs.dispatch_timeout,
            retry_delay=jobs.retry_delay,
        )
        if not isinstance(self.store, GuardedTransactions):
            self.store = GuardedTransactions(self.store)
        self.deletion_ledger = deletion_ledger

    def _live(self, tx: Transaction, lease: Lease) -> Job | None:
        job = super()._live(tx, lease)
        if job is None:
            return None
        deadline = job.lease_expires_at
        assert deadline is not None

        def current() -> None:
            if normalize_utc(self.now()) >= deadline:
                raise LeaseLost()

        # Retain the original expiry through slow ledger/DB reads, every physical
        # write, transaction callback completion and delayed response release.
        protect(tx, current)
        return job

    def accepted(self, tx: Transaction, job: Job) -> tuple[DeletionIntent, Document]:
        if job.kind != KIND or job.attributes != {"dispatch_queue": KIND} or job.output is not None:
            raise ValueError("Invalid purge envelope")
        try:
            intent = self.deletion_ledger.get(job.operation_id)
        except LedgerError:
            raise AuthorizationUnavailable() from None
        saved = tx.get(operation_path(job.tenant_id, job.operation_id))
        if (
            intent is None
            or intent.tenant_id != job.tenant_id
            or saved is None
            or saved.get("intent") != intent.model_dump(mode="json")
            or saved.get("tenant_id") != job.tenant_id
            or saved.get("request_hash") != intent.request_hash
            or saved.get("operation", {}).get("operation_id") != intent.operation_id
            or saved.get("operation", {}).get("kind") != "forget_memory"
            or job.grant.ref.kind != "facts"
            or job.grant.ref.id != intent.targets[0].resource_id
        ):
            raise ValueError("Unaccepted deletion")
        for target in intent.targets:
            marker = tx.get(
                tombstone_path(
                    intent.tenant_id,
                    {"fact": "facts", "message": "messages", "note": "notes"}[target.kind],
                    target.resource_id,
                    target.parent_id,
                )
            )
            if marker is None:
                raise ValueError("Missing deletion fence")
            recorded = marker.get("target", {})
            if target.kind == "fact":
                valid = (
                    marker.get("operation_id") == intent.operation_id
                    and marker.get("mode") == "delete"
                    and recorded == target.model_dump(mode="json")
                )
            else:
                # Another accepted forget may retain an older suppress marker or
                # supersede it with a stronger delete. Neither reauthorizes content.
                valid = (
                    recorded.get("kind") == target.kind
                    and recorded.get("resource_id") == target.resource_id
                    and recorded.get("parent_id") == target.parent_id
                    and marker.get("mode") in ("suppress_reuse", "delete")
                    and (
                        intent.source_policy == "suppress_reuse"
                        or (
                            marker.get("mode") == "delete"
                            and recorded == target.model_dump(mode="json")
                            and marker.get("operation_id") == intent.operation_id
                        )
                    )
                )
            if not valid:
                raise ValueError("Changed deletion fence")
        return intent, saved

    def _invalid_reason(self, tx: Transaction, job: Job) -> Reason | None:
        try:
            self.accepted(tx, job)
        except (ValueError, ValidationError):
            return "invalid_job"
        return None

    def activate(self, tenant: ResourceId, identity: ResourceId) -> bool:
        def work(tx: Transaction) -> bool:
            job = self._read(tx, tenant, identity)
            if job is None or job.kind != KIND or job.status != "waiting":
                return False
            reason = self._invalid_reason(tx, job)
            if reason is not None:
                self._stop(tx, job, "failed", reason)
                return False
            self._write(tx, job, status="pending")
            return True

        return self.store.run(work)

    def recover_expired(self, tenant: ResourceId, identity: ResourceId) -> bool:
        """Refund interrupted deletion attempts only; no external effect checkpoint."""

        def work(tx: Transaction) -> bool:
            job = self._read(tx, tenant, identity)
            now = normalize_utc(self.now())
            if job is None or job.kind != KIND or job.checkpoints:
                return False
            expired = (
                job.status == "running"
                and job.lease_expires_at is not None
                and job.lease_expires_at <= now
            )
            exhausted = job.status == "failed" and job.reason == "attempts_exhausted"
            if not (expired or exhausted):
                return False
            _, saved = self.accepted(tx, job)
            if saved["operation"]["status"] == "succeeded":
                return False
            saved.setdefault("purge_accepted_at", saved["operation"]["updated_at"])
            saved.setdefault("purge", {})["retry_reason"] = "lease_expired"
            saved["purge_recoveries"] = saved.get("purge_recoveries", 0) + 1
            saved["operation"].update(status="running", updated_at=now.isoformat())
            tx.set(operation_path(tenant, job.operation_id), saved)
            self._write(
                tx,
                job,
                status="pending",
                attempts=0,
                lease_expires_at=None,
                dispatch_after=now,
                available_at=now,
                dispatch_pending=False,
                reason="lease_expired",
            )
            return True

        return self.store.run(work)

    def revalidate_completed(self, tenant: ResourceId, identity: ResourceId) -> bool:
        """Explicit migration of old purge completion; never ordinary terminal Jobs.

        Old verified/succeeded flags cannot certify the current inventory. Retain
        admission bounds and counters, but rescan and reconcile both hooks with the
        same stable keys before issuing a new completion witness.
        """

        def work(tx: Transaction) -> bool:
            job = self._read(tx, tenant, identity)
            if job is None or job.kind != KIND or job.status != "succeeded" or job.checkpoints:
                return False
            _, saved = self.accepted(tx, job)
            state = saved.get("purge", {})
            if (
                saved["operation"]["status"] != "succeeded"
                or state.get("schema_version") != 1
                or state.get("locally_verified") is True
            ):
                return False
            now = normalize_utc(self.now())
            state.update(
                stack=[],
                pass_changes=1,
                verified=False,
                locally_verified=False,
                receipts={},
                legacy_pending=False,
                retry_reason="legacy_completion_revalidation",
            )
            saved["operation"].update(status="running", updated_at=now.isoformat())
            tx.set(operation_path(tenant, job.operation_id), saved)
            self._write(
                tx,
                job,
                status="pending",
                attempts=0,
                lease_expires_at=None,
                available_at=now,
                dispatch_after=now,
                dispatch_pending=False,
                dispatch_generation=job.dispatch_generation + 1,
                lease_generation=job.lease_generation + 1,
                reason=None,
            )
            return True

        return self.store.run(work)

    def prepare_dispatch(
        self, tenant: ResourceId, identity: ResourceId, *, queue_name: str | None = None
    ) -> Delivery | None:
        if queue_name not in (None, KIND):
            return None
        self.recover_expired(tenant, identity)
        return super().prepare_dispatch(tenant, identity, queue_name=queue_name)

    def defer_unavailable(
        self,
        lease: Lease,
        *,
        reason: Literal["authorization_unavailable", "persistence_unavailable"],
    ) -> None:
        super().defer_unavailable(lease, reason=reason)

        def record(tx: Transaction) -> None:
            job = self._read(tx, lease.tenant_id, lease.job_id)
            if (
                job is None
                or job.kind != KIND
                or job.lease_generation != lease.generation
                or job.status != "pending"
                or job.reason != reason
            ):
                return
            saved = tx.get(operation_path(job.tenant_id, job.operation_id))
            if saved is None or saved.get("operation", {}).get("status") == "succeeded":
                return
            saved.setdefault("purge_accepted_at", saved["operation"]["updated_at"])
            saved.setdefault("purge", {})["retry_reason"] = reason
            saved["operation"]["updated_at"] = normalize_utc(self.now()).isoformat()
            tx.set(operation_path(job.tenant_id, job.operation_id), saved)

        self.store.run(record)

    def fail(self, lease: Lease, *, retry: bool = False) -> None:
        def work(tx: Transaction) -> None:
            job = self._live(tx, lease)
            if job is None:
                return
            _, saved = self.accepted(tx, job)
            now = normalize_utc(self.now())
            reason = "page_budget" if retry else "handler_error"
            saved.setdefault("purge", {})["retry_reason"] = (
                saved.get("purge", {}).get("retry_reason") or reason
            )
            saved["purge"]["overdue"] = now - job.created_at > timedelta(hours=24)
            saved.setdefault("purge_accepted_at", saved["operation"]["updated_at"])
            saved["operation"].update(status="running", updated_at=now.isoformat())
            tx.set(operation_path(job.tenant_id, job.operation_id), saved)
            # A resumable deletion is not exhausted by ordinary pagination/outages.
            self._write(
                tx,
                job,
                status="pending",
                attempts=max(0, job.attempts - 1),
                lease_expires_at=None,
                available_at=now + self.retry_delay,
                dispatch_after=now + self.retry_delay,
                dispatch_pending=False,
                reason="retry_requested" if retry else "handler_error",
            )

        self.store.run(work)

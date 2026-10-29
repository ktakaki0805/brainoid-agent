"""Bounded recovery of quota slots; expiration fences the canonical run/job first."""

from datetime import datetime

from brainoid_contracts import ResourceId, normalize_utc
from brainoid_contracts.transactions import Transaction
from brainoid_jobs import Jobs
from brainoid_jobs.models import TERMINAL, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_runs.control import cancellation
from brainoid_runs.state import KIND, run_path
from brainoid_usage import UsageLedger
from brainoid_usage.ledger import run_path as usage_path

from brainoid_worker.dispatcher import JobPages


class UsageReconciler:
    def __init__(self, jobs: Jobs) -> None:
        self.jobs = jobs
        self.usage = UsageLedger(jobs.store, now=jobs.now)

    def recover(
        self, tenant: ResourceId, pages: JobPages, *, after: str | None = None, limit: int = 100
    ) -> str | None:
        if not 1 <= limit <= 100 or (
            after is not None and (not after or "/" in after or len(after) > 128)
        ):
            raise ValueError("invalid_usage_page")
        documents = pages.page(f"tenants/{tenant}/usage_runs", after=after, limit=limit)
        for path, doc in documents:
            run = ResourceId(doc["run_id"])
            if path != usage_path(tenant, run) or doc["tenant_id"] != tenant:
                raise ValueError("invalid_usage_path")
            self.reconcile(tenant, run)
        return documents[-1][0].rsplit("/", 1)[-1] if len(documents) == limit else None

    def reconcile(
        self, tenant: ResourceId, run: ResourceId, *, provider_stopped: bool = False
    ) -> None:
        def work(tx: Transaction) -> None:
            quota = tx.get(usage_path(tenant, run))
            if quota is None or not quota["active"]:
                return
            if quota.get("purpose") == "audio_input":
                # STT has no conversation run/job and cannot be reexecuted: its raw
                # bytes were memory-only. Expiry fences any late API publication.
                if normalize_utc(self.jobs.now()) >= datetime.fromisoformat(quota["expires_at"]):
                    self.usage.prepare_release(tx, tenant, run, abandon=True)()
                return
            job = tx.get(job_path(tenant, job_id(tenant, KIND, run)))
            record = tx.get(run_path(tenant, run))
            now = normalize_utc(self.jobs.now())
            expired = now >= datetime.fromisoformat(quota["expires_at"])
            terminal = job is not None and job["status"] in TERMINAL
            if not terminal and not expired:
                return
            # A live provider's reservation remains charged. A terminal job or the
            # hard run deadline permits reclaiming the logical execution slot; the
            # durable started checkpoint is retained and can never be reexecuted.
            release = self.usage.prepare_release(
                tx, tenant, run, abandon=expired or provider_stopped
            )
            if expired and not terminal and record is not None:
                # cancellation reads all business records before it writes. It may
                # itself prepare a release; with started reservations it leaves it
                # to the above abandonment closure.
                cancellation(tx, self.jobs, record)
            release()

        self.jobs.store.run(work)

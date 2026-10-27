"""Confirmed Calendar job, single mutation start and conservative read-only recovery."""

from datetime import timedelta
from typing import TYPE_CHECKING

from brainoid_agent_core.types import ToolResult
from brainoid_contracts import ResourceId
from brainoid_contracts.errors import APIError, ErrorCode
from brainoid_contracts.guards import GuardedTransactions, protect
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost, RetryableError
from brainoid_jobs.models import TERMINAL, job_id
from brainoid_jobs.repository import path as job_path
from brainoid_memory.calendar_access import current_integration
from brainoid_memory.calendar_proposal_access import check_origin
from brainoid_memory.calendar_proposals import CalendarProposals, proposal_path
from brainoid_memory.calendar_reader import CalendarQuery, CalendarReader
from brainoid_memory.calendar_writer import (
    KIND,
    CalendarWriter,
    ProviderReply,
    body,
    event_id,
    matches,
    supported,
)
from brainoid_memory.forget import operation_path
from brainoid_memory.sources import CanonicalSources
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.memory_sources import SavedMessages
from brainoid_runs.state import KIND as RUN_KIND
from brainoid_runs.state import RunState, run_path
from pydantic import JsonValue

from brainoid_worker.entry import Execution
from brainoid_worker.resume_run import Confirmations, ExternalClaim

if TYPE_CHECKING:
    from brainoid_worker.dispatcher import JobPages


class CalendarWrite:
    def __init__(
        self,
        jobs: Jobs,
        reader: CalendarReader,
        provider: CalendarWriter,
        *,
        state: RunState,
        confirmations: Confirmations | None = None,
    ) -> None:
        self.jobs, self.reader, self.provider = jobs, reader, provider
        self.state, self.confirmations = state, confirmations
        self.store = GuardedTransactions(jobs.store)

    def current(
        self, tx: Transaction, job: Job, *, writing: bool = True
    ) -> tuple[Document, Document]:
        record = CalendarProposals(self.reader).read(tx, job.tenant_id, job.operation_id)
        if record.get("operation_id") != job.operation_id or record["status"] not in (
            "confirmed",
            "executing",
            "outcome_unknown",
        ):
            raise PolicyDenied()
        policy = self.jobs._policy(tx)
        principal = policy.principal(job.tenant_id, job.grant.principal.subject)
        integration, scope = current_integration(
            tx,
            policy,
            principal,
            ResourceId(record["integration_id"]),
            calendar=record["normalized_request"]["calendar_id"],
            operation="calendar.propose",
        )
        CalendarProposals.authorized(record, integration, scope)
        check_origin(
            tx,
            policy,
            record,
            now=self.jobs.now,
            credential_epoch=self.state.credential_epoch,
            sources=CanonicalSources(SavedMessages()),
            for_model=True,
        )
        if record.get("run_id") is not None and writing:
            run = tx.get(run_path(job.tenant_id, record["run_id"]))
            if run is None or run.get("status") == "cancelled":
                raise PolicyDenied()
            proposal = tx.get(f"tenants/{job.tenant_id}/run_proposals/{record['id']}")
            if proposal is None:
                raise RetryableError()
            saved = tx.get(
                job_path(
                    job.tenant_id, job_id(job.tenant_id, RUN_KIND, ResourceId(record["run_id"]))
                )
            )
            if (
                saved is None
                or proposal.get("external_claim") != record.get("external_claim")
                or proposal.get("status") != "external_started"
            ):
                raise PolicyDenied()
            self.state.check(tx, Job.model_validate(saved), waiting=True)
        return record, integration

    def fence(self, execution: Execution) -> tuple[Document, Document]:
        def read(tx: Transaction) -> tuple[Document, Document]:
            job = self.jobs._live(tx, execution.lease)
            if job is None or job.lease_expires_at is None:
                raise LeaseLost()
            deadline = job.lease_expires_at

            def live() -> None:
                if self.jobs.now() >= deadline:
                    raise LeaseLost()

            protect(tx, live)
            return self.current(tx, job)

        return self.store.run(read)

    def run(self, execution: Execution) -> JsonValue:
        try:
            return self._run(execution)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except APIError as error:
            if error.code == ErrorCode.TEMPORARILY_UNAVAILABLE:
                raise RetryableError() from None
            self.deny(execution)
            raise LeaseLost() from None
        except PolicyDenied:
            self.deny(execution)
            raise LeaseLost() from None

    def deny(self, execution: Execution) -> None:
        job = self.jobs.get(execution.lease.tenant_id, execution.lease.job_id)
        if job is not None:
            self.record(job, "failed", "permission_denied", require_started=False)
        self.jobs.obsolete(execution.lease)

    def _run(self, execution: Execution) -> JsonValue:
        job = execution.check()
        record, integration = self.fence(execution)
        token = self.reader.token(integration)
        change = record["normalized_request"]
        self.fence(execution)
        now = self.jobs.now()
        query = CalendarQuery(
            calendar_id=change["calendar_id"],
            time_min=now,
            time_max=now + timedelta(seconds=1),
            limit=1,
        )

        def before_send() -> None:
            self.fence(execution)

        acl = self.reader.fetch(integration, query, None, before_send)
        self.fence(execution)
        roles = (
            {"owner", "writer", "writerWithoutPrivateAccess"}
            if change["action"] == "create"
            else {"owner", "writer"}
        )
        if acl.get("accessRole") not in roles:
            raise PolicyDenied()
        original = None
        if change["action"] != "create":
            self.fence(execution)
            target = self.provider.inspect(token, change["calendar_id"], event_id(record))
            self.fence(execution)
            if target.status != 200 or target.body is None:
                self.record(job, "failed", "target_unavailable", require_started=False)
                return {"status": "failed"}
            original = target.body
            if (
                original.get("id") != event_id(record)
                or original.get("etag") != change["provider_etag"]
            ):
                self.record(job, "failed", "conflict", require_started=False)
                return {"status": "failed"}
            if not supported(original):
                raise PolicyDenied()

        def invoke(_: str) -> JsonValue:
            # The durable start proves only that a call might happen. Check again
            # after checkpoint commit, secret access, provider reads, and before send.
            self.fence(execution)

            def start(tx: Transaction) -> None:
                live = self.jobs._live(tx, execution.lease)
                if live is None:
                    raise LeaseLost()
                current, _ = self.current(tx, live)
                saved = tx.get(operation_path(job.tenant_id, job.operation_id))
                if (
                    saved is None
                    or saved.get("started_at") is not None
                    or saved.get("operation", {}).get("status") != "pending"
                ):
                    raise LeaseLost()
                tx.set(
                    operation_path(job.tenant_id, job.operation_id),
                    {
                        **saved,
                        "started_at": self.jobs.now().isoformat(),
                        "operation": {
                            **saved["operation"],
                            "status": "running",
                            "updated_at": self.jobs.now().isoformat(),
                        },
                    },
                )
                tx.set(
                    proposal_path(job.tenant_id, job.operation_id),
                    {**current, "status": "executing"},
                )

            self.store.run(start)
            self.fence(execution)
            try:
                reply = self.provider.write(
                    token,
                    change["action"],
                    change["calendar_id"],
                    event_id(record),
                    change.get("provider_etag"),
                    body(record, original),
                )
            except Exception:
                reply = ProviderReply(0)
            if (change["action"] == "delete" and reply.status == 204) or matches(record, reply):
                self.record(job, "succeeded", None)
            elif reply.status in (409, 412):
                self.record(job, "failed", "conflict")
            elif reply.status in (400, 401, 403, 404, 410):
                self.record(job, "failed", "provider_rejected")
            else:
                # A timeout/5xx is not a negative acknowledgment. Reconcile once
                # with current authority; never issue a second mutation.
                self.resolve(job)
            return {"operation_id": job.operation_id}

        return execution.effect("calendar.write", invoke)

    def record(
        self, job: Job, status: str, error: str | None, *, require_started: bool = True
    ) -> None:
        """Store content-free observed facts even if the lease or source was revoked."""

        self.jobs.store.run(
            lambda tx: self._record(tx, job, status, error, require_started=require_started)
        )

    def _record(
        self,
        tx: Transaction,
        job: Job,
        status: str,
        error: str | None,
        *,
        require_started: bool,
    ) -> bool:
        path = operation_path(job.tenant_id, job.operation_id)
        saved = tx.get(path)
        calendar = tx.get(proposal_path(job.tenant_id, job.operation_id))
        if saved is None or saved.get("operation", {}).get("kind") != KIND:
            return False
        if require_started and saved.get("started_at") is None:
            raise PolicyDenied()
        if saved["operation"]["status"] in ("succeeded", "failed"):
            return False
        if not require_started and saved.get("started_at") is not None:
            return False
        updated = {
            **saved["operation"],
            "status": status,
            "updated_at": self.jobs.now().isoformat(),
        }
        if error is not None:
            updated["error_code"] = error
        else:
            updated.pop("error_code", None)
        tx.set(path, {**saved, "operation": updated})
        if calendar is not None and calendar.get("args_hash") == saved.get("args_hash"):
            # Never reconstruct deleted private content from an in-flight snapshot.
            proposal = {**calendar, "status": status}
            if error is not None:
                proposal["error_code"] = error
            else:
                proposal.pop("error_code", None)
            tx.set(proposal_path(job.tenant_id, job.operation_id), proposal)
        return True

    def close_unstarted(self, job: Job) -> bool:
        """Close work that can no longer send, atomically with the current job fence.

        The business start marker is committed before every provider mutation.
        Its absence proves no mutation was sent even if Jobs saved STARTED first.
        A live lease, pending retry or pre-suspend admission is not terminal evidence.
        """

        def close(tx: Transaction) -> bool:
            current = self.jobs._read(tx, job.tenant_id, job.id)
            if current is None or current.kind != KIND or current.operation_id != job.operation_id:
                return False
            expired = (
                current.status == "running"
                and current.lease_expires_at is not None
                and current.lease_expires_at <= self.jobs.now()
                and (
                    any(c.state == "started" for c in current.checkpoints)
                    or current.attempts >= current.max_attempts
                )
            )
            if current.status not in TERMINAL and not expired:
                return False
            written = self._record(tx, current, "failed", "not_started", require_started=False)
            if written and expired:
                self.jobs._stop(tx, current, "failed", "outcome_unknown")
            return True

        return self.jobs.store.run(close)

    def resolve(self, job: Job) -> None:
        saved = self.jobs.store.run(
            lambda tx: tx.get(operation_path(job.tenant_id, job.operation_id))
        )
        if saved is None or saved["operation"]["status"] in ("succeeded", "failed"):
            return
        if saved.get("started_at") is None:
            if self.close_unstarted(job):
                return
            try:
                self.store.run(lambda tx: self.current(tx, job))
            except PolicyUnavailable:
                raise AuthorizationUnavailable() from None
            except (PolicyDenied, APIError):
                self.record(job, "failed", "permission_denied", require_started=False)
            except RetryableError:
                pass
            return
        try:
            record, integration = self.store.run(lambda tx: self.current(tx, job, writing=False))
            token = self.reader.token(integration)
            self.store.run(lambda tx: self.current(tx, job, writing=False))
            try:
                reply = self.provider.inspect(
                    token, record["normalized_request"]["calendar_id"], event_id(record)
                )
            except Exception:
                reply = ProviderReply(0)
            self.store.run(lambda tx: self.current(tx, job, writing=False))
            matched = matches(record, reply)
        except PolicyUnavailable:
            raise AuthorizationUnavailable() from None
        except (PolicyDenied, APIError, RetryableError):
            matched = False
        self.record(
            job,
            "succeeded" if matched else "outcome_unknown",
            None if matched else "outcome_unknown",
        )

    def commit(self, tx: Transaction, job: Job, result: JsonValue) -> None:
        pass  # Fact persistence is independent of lease lifetime; no external I/O.

    def reconcile(self, job: Job) -> None:
        self.resolve(job)
        if self.confirmations is None:
            return

        def read(tx: Transaction):  # type: ignore[no-untyped-def]
            return (
                tx.get(operation_path(job.tenant_id, job.operation_id)),
                tx.get(proposal_path(job.tenant_id, job.operation_id)),
            )

        saved, calendar = self.jobs.store.run(read)
        if saved is None or calendar is None or calendar.get("external_claim") is None:
            return
        status = saved["operation"]["status"]
        if status not in ("succeeded", "failed", "outcome_unknown"):
            return
        claim = ExternalClaim.model_validate(calendar["external_claim"])
        result = ToolResult(
            tool_call_id=claim.tool_call_id,
            operation_id=claim.effect_id,
            status=status,
            result={},
            error_code=None
            if status != "failed"
            else "conflict"
            if saved["operation"].get("error_code") == "conflict"
            else "unavailable",
        )
        # Existing terminal delivery is never consumed twice, including after a
        # later business-only refinement of an unknown result.
        proposal = self.jobs.store.run(
            lambda tx: tx.get(f"tenants/{job.tenant_id}/run_proposals/{claim.proposal_id}")
        )
        if proposal is not None and proposal.get("supplied_result") is None:
            self.confirmations.receive_external(claim, result)

    def recover(
        self, tenant: ResourceId, pages: "JobPages", *, after: str | None = None, limit: int = 100
    ) -> str | None:
        if not 1 <= limit <= 100:
            raise ValueError("Invalid recovery page")
        records = pages.page(f"tenants/{tenant}/jobs", after=after, limit=limit)
        for _, document in records:
            if document.get("kind") == KIND:
                self.reconcile(Job.model_validate(document))
        return records[-1][0].rsplit("/", 1)[-1] if len(records) == limit else None

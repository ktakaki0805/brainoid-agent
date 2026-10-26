"""One-shot confirmation decisions and lease-free run checkpoint suspension.

These are internal functions. Calendar HTTP confirmation and provider writes are
separate business adapters. The canonical job is requeued with a fresh generation.
"""

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Literal
from uuid import uuid4

from brainoid_agent_core.types import ToolExecution, ToolResult
from brainoid_agent_core.types.common import CoreValue
from brainoid_contracts import ResourceId, normalize_utc
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable, Job, Jobs, LeaseLost
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_policy.accounts import AccountUnavailable
from brainoid_policy.models import Action, Subject, UserSubject
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs import events
from brainoid_runs.state import KIND, RunState, run_path
from brainoid_tools import (
    Arguments,
    Authority,
    Registry,
    ResultAuthorizer,
    ResultConsumer,
    ToolRejected,
)
from brainoid_tools.executor import Authorizer, execution_fingerprint
from brainoid_tools.results import HandlerResult, correlate
from brainoid_tools.reuse import receipt
from pydantic import Field

from brainoid_worker.dispatcher import JobPages
from brainoid_worker.entry import Execution


def proposal_path(tenant: ResourceId, proposal: ResourceId) -> str:
    return f"tenants/{tenant}/run_proposals/{proposal}"


class ExternalClaim(CoreValue):
    """Internal durable binding, never an HTTP/model input or repeat-I/O permission."""

    id: ResourceId
    tenant_id: ResourceId
    run_id: ResourceId
    proposal_id: ResourceId
    tool_call_id: ResourceId
    effect_id: ResourceId
    fingerprint: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def result_snapshot(result: ToolResult) -> ToolResult:
    supplied = correlate(
        HandlerResult(
            status=result.status,
            result=result.result,
            error_code=result.error_code,
            proposal_id=result.proposal_id,
        ),
        result.tool_call_id,
        result.operation_id or result.command_id or result.tool_call_id,
        "operation" if result.operation_id is not None else "command",
    )
    if supplied != result or supplied.status not in ("succeeded", "failed", "outcome_unknown"):
        raise PolicyDenied()
    return supplied


class RunToolAuthority:
    """Add immutable run/lease/call bindings to a business-specific target authorizer."""

    def __init__(self, state: RunState, job: Job, delegate: Authorizer) -> None:
        self.state, self.job, self.delegate = state, job, delegate
        self.unavailable: Exception | None = None

    def check(self, tx: Transaction, request: ToolExecution, arguments: Arguments) -> Authority:
        try:
            run, _, _, _ = self.state.check(tx, self.job)
        except PolicyUnavailable:
            self.unavailable = AuthorizationUnavailable()
            raise self.unavailable from None
        except PersistenceUnavailable as error:
            self.unavailable = error
            raise
        except PolicyDenied:
            raise ToolRejected("permission_denied") from None
        if request.tool_call_id not in run.get("tool_call_ids", []):
            raise ToolRejected("permission_denied")
        authority = self.delegate.check(tx, request, arguments)
        if (
            authority.run_id != self.job.operation_id
            or authority.context != self.job.grant.context
            or request.call.name not in run["agent"]["allowed_tools"]
        ):
            raise ToolRejected("permission_denied")
        if authority.proposal_id is not None:
            proposal = tx.get(proposal_path(self.job.tenant_id, authority.proposal_id))
            if proposal is not None:
                if (
                    proposal["run_id"] != self.job.operation_id
                    or proposal["tool_call_id"] != request.tool_call_id
                ):
                    raise ToolRejected("permission_denied")
                if (
                    proposal["status"] == "confirmed"
                    and proposal.get("resume_mode", "execute") == "execute"
                ):
                    authority = authority.model_copy(update={"confirmation": "confirmed"})
                elif proposal["status"] not in ("pending", "confirmed"):
                    raise ToolRejected("permission_denied")
        return authority


class Confirmations:
    def __init__(
        self,
        jobs: Jobs,
        state: RunState,
        *,
        registry: Registry | None = None,
        result_authorizer: ResultAuthorizer | None = None,
    ) -> None:
        self.jobs, self.state = jobs, state
        self.registry, self.result_authorizer = registry, result_authorizer

    def suspend(self, execution: Execution, request: ToolExecution, result: ToolResult) -> None:
        assert result.proposal_id is not None

        def work(tx: Transaction) -> None:
            assert result.proposal_id is not None
            job = self.jobs._live(tx, execution.lease)
            if job is None:
                raise LeaseLost()
            run, _, _, _ = self.state.check(tx, job)
            path = proposal_path(job.tenant_id, result.proposal_id)
            existing = tx.get(path)
            tool = tx.get(f"tenants/{job.tenant_id}/tool_executions/{request.tool_call_id}")
            if (
                existing is not None
                or tool is None
                or tool["result"] != result.model_dump(mode="json")
            ):
                raise PolicyDenied()
            version = run.get("checkpoint_version", 0) + 1
            proposal = {
                "id": result.proposal_id,
                "run_id": job.operation_id,
                "tool_call_id": request.tool_call_id,
                "checkpoint_version": version,
                "fingerprint": tool["fingerprint"],
                "request": request.model_dump(mode="json"),
                # An executed proposal-producing handler must never be called again
                # to perform the separately proposed action. Only its result may resume.
                "resume_mode": "execute"
                if tool["authority"]["confirmation"] == "waiting"
                else "result",
                "status": "pending",
                "expires_at": (self.jobs.now() + timedelta(minutes=5)).isoformat(),
            }
            model_key = "model" if run.get("model_step", 0) == 0 else f"model.{run['model_step']}"
            checkpoint = next(c for c in job.checkpoints if c.key == model_key)
            if checkpoint.state != "completed" or not isinstance(checkpoint.result, dict):
                raise PolicyDenied()
            text = run.get("completed_text", "") + str(checkpoint.result.get("text", ""))
            prefix = run.get("streamed_text", "")
            if not text.startswith(prefix):
                raise PolicyDenied()
            events.append_text(tx, run, text[len(prefix) :], self.jobs.now())
            tx.create(path, proposal)
            tx.set(
                run_path(job.tenant_id, job.operation_id),
                {
                    **run,
                    "status": "waiting_confirmation",
                    "waiting_proposal_id": result.proposal_id,
                    "checkpoint_version": version,
                    "version": run["version"] + 1,
                },
            )
            self.jobs._write(
                tx,
                job,
                status="waiting",
                lease_expires_at=None,
                lease_generation=job.lease_generation + 1,
                dispatch_pending=False,
            )

        self.jobs.store.run(work)
        raise LeaseLost()

    def _access(self, tx: Transaction, job: Job, subject: Subject | None) -> None:
        policy = self.jobs._policy(tx)
        current = policy.resource(
            job.tenant_id,
            job.grant.principal.subject,
            job.grant.ref,
            action=Action(job.grant.action),
            output=job.output,
        )
        if (
            current.principal.stamp != job.grant.principal.stamp
            or current.output_stamp != job.grant.output_stamp
        ):
            raise PolicyDenied()
        try:
            if self.jobs._accounts(tx, job.grant, job.output) != job.account_stamps:
                raise PolicyDenied()
        except AccountUnavailable:
            raise PolicyDenied() from None
        if subject is not None:
            actor, initiator = policy.principal(job.tenant_id, subject), job.grant.principal
            if actor.subject != initiator.subject and not (
                isinstance(actor.subject, UserSubject)
                and actor.user_id is not None
                and actor.user_id == initiator.user_id
            ):
                raise PolicyDenied()

    def _read(
        self, tx: Transaction, tenant: ResourceId, run_id: ResourceId, proposal_id: ResourceId
    ) -> tuple[Job, Document, Document]:
        proposal = tx.get(proposal_path(tenant, proposal_id))
        document = tx.get(job_path(tenant, job_id(tenant, KIND, run_id)))
        if proposal is None or proposal["run_id"] != run_id or document is None:
            raise PolicyDenied()
        tool = tx.get(f"tenants/{tenant}/tool_executions/{proposal['tool_call_id']}")
        job = Job.model_validate(document)
        if (
            job.tenant_id != tenant
            or job.operation_id != run_id
            or tool is None
            or tool["fingerprint"] != proposal["fingerprint"]
            or tool["authority"]["run_id"] != run_id
        ):
            raise PolicyDenied()
        return job, proposal, tool

    def _waiting(self, tx: Transaction, job: Job, proposal: Document) -> Document:
        run, _, _, _ = self.state.check(tx, job, waiting=True)
        if (
            run.get("waiting_proposal_id") != proposal["id"]
            or run.get("checkpoint_version") != proposal["checkpoint_version"]
        ):
            raise PolicyDenied()
        return run

    def _resume(self, tx: Transaction, job: Job, run: Document, decision: str) -> None:
        tx.set(
            run_path(job.tenant_id, job.operation_id),
            {
                **run,
                "status": "queued",
                "confirmation_decision": decision,
                "version": run["version"] + 1,
            },
        )
        self.jobs._write(
            tx,
            job,
            status="pending",
            available_at=self.jobs.now(),
            dispatch_after=self.jobs.now(),
            dispatch_pending=False,
            dispatch_generation=job.dispatch_generation + 1,
            lease_generation=job.lease_generation + 1,
            attempts=max(0, job.attempts - 1),
        )

    def decide(
        self,
        tenant: ResourceId,
        run_id: ResourceId,
        proposal_id: ResourceId,
        decision: Literal["confirmed", "declined", "expired"],
        *,
        subject: Subject | None = None,
        result: ToolResult | None = None,
    ) -> str:
        # External facts require a prior atomic claim and the separate receipt boundary.
        if result is not None or (decision != "expired" and subject is None):
            raise PolicyDenied()

        def work(tx: Transaction) -> str:
            job, proposal, tool = self._read(tx, tenant, run_id, proposal_id)
            self._access(tx, job, subject)
            if proposal["status"] != "pending":
                if (
                    proposal.get("decision") != decision
                    or proposal.get("external_claim") is not None
                ):
                    raise PolicyDenied()
                return str(proposal["status"])
            run = self._waiting(tx, job, proposal)
            if (
                tool["result"] is None
                or tool["result"]["status"] != "waiting_confirmation"
                or tool["result"]["proposal_id"] != proposal_id
            ):
                raise PolicyDenied()
            expired = normalize_utc(proposal["expires_at"]) <= self.jobs.now()
            if decision == "expired" and not expired:
                raise PolicyDenied()
            chosen = "expired" if expired else decision
            if chosen == "confirmed" and proposal.get("resume_mode", "execute") != "execute":
                raise PolicyDenied()
            tx.set(
                proposal_path(tenant, proposal_id),
                {
                    **proposal,
                    "status": chosen,
                    "decision": decision,
                },
            )
            if chosen == "confirmed":
                tx.set(
                    f"tenants/{tenant}/tool_executions/{proposal['tool_call_id']}",
                    {
                        **tool,
                        "state": "ready",
                        "result": None,
                        "confirmation_result": tool["result"],
                        "authority": {**tool["authority"], "confirmation": "confirmed"},
                    },
                )
            else:
                denied = correlate(
                    HandlerResult(status="failed", error_code="rejected"),
                    ResourceId(tool["tool_call_id"]),
                    ResourceId(tool["effect_id"]),
                    tool["effect_kind"],
                )
                tx.set(
                    f"tenants/{tenant}/tool_executions/{proposal['tool_call_id']}",
                    {
                        **tool,
                        "state": "finished",
                        "result": denied.model_dump(mode="json"),
                        "confirmation_result": tool["result"],
                    },
                )
            self._resume(tx, job, run, chosen)
            return chosen

        return self.jobs.store.run(work)

    def start_external(
        self,
        tenant: ResourceId,
        run_id: ResourceId,
        proposal_id: ResourceId,
        *,
        subject: Subject,
        prepare: Callable[[Transaction, ToolExecution, ExternalClaim], None],
    ) -> ExternalClaim:
        """Confirm and enqueue the business effect once, before any external I/O.

        prepare must validate current business target/intent/authority and persist the
        canonical business job/outbox in this transaction (reads before writes, no I/O).
        Replays return the original binding but never call prepare again. The binding
        is for reconciliation/results; only the business job's own lease/start fence
        permits I/O. A lost commit response must not cause inline effect execution.
        """
        claim_id = ResourceId(str(uuid4()))

        def work(tx: Transaction) -> ExternalClaim:
            job, proposal, tool = self._read(tx, tenant, run_id, proposal_id)
            self._access(tx, job, subject)
            if proposal.get("external_claim") is not None:
                return ExternalClaim.model_validate(proposal["external_claim"])
            self._waiting(tx, job, proposal)
            if (
                proposal["status"] != "pending"
                or normalize_utc(proposal["expires_at"]) <= self.jobs.now()
                or tool.get("result", {}).get("status") != "waiting_confirmation"
                or tool["result"]["proposal_id"] != proposal_id
                or self.registry is None
            ):
                raise PolicyDenied()
            request = ToolExecution.model_validate(proposal["request"])
            _, _, canonical = self.registry.validate(request.call)
            if (
                request.context != job.grant.context
                or request.tool_call_id != tool["tool_call_id"]
                or execution_fingerprint(request, canonical) != tool["fingerprint"]
            ):
                raise PolicyDenied()
            claim = ExternalClaim(
                id=claim_id,
                tenant_id=tenant,
                run_id=run_id,
                proposal_id=proposal_id,
                tool_call_id=ResourceId(tool["tool_call_id"]),
                effect_id=ResourceId(tool["effect_id"]),
                fingerprint=tool["fingerprint"],
            )
            prepare(tx, request.model_copy(deep=True), claim.model_copy(deep=True))
            tx.set(
                proposal_path(tenant, proposal_id),
                {
                    **proposal,
                    "status": "external_started",
                    "decision": "confirmed",
                    "external_claim": claim.model_dump(mode="json"),
                    "external_started_at": self.jobs.now().isoformat(),
                },
            )
            tx.set(
                f"tenants/{tenant}/tool_executions/{claim.tool_call_id}",
                {
                    **tool,
                    "state": "started",
                    "result": None,
                    "confirmation_result": tool["result"],
                    "external_claim": claim.model_dump(mode="json"),
                },
            )
            return claim

        return self.jobs.store.run(work)

    def receive_external(self, claim: ExternalClaim, result: ToolResult) -> str:
        """Record a claimed effect even after cancellation/revocation; resume only if live.

        This trusted business-worker boundary accepts no user/model-selected claim.
        Revoked sources/consumers retain only content-free outcome facts, never payload.
        Unknown is immutable here; later reconciliation belongs to the business operation.
        """
        claim = ExternalClaim.model_validate(claim.model_dump()).model_copy(deep=True)
        supplied = result_snapshot(result)

        def work(tx: Transaction) -> str:
            tenant, run_id, proposal_id = claim.tenant_id, claim.run_id, claim.proposal_id
            job, proposal, tool = self._read(tx, tenant, run_id, proposal_id)
            if (
                proposal.get("external_claim") != claim.model_dump(mode="json")
                or tool.get("external_claim") != claim.model_dump(mode="json")
                or claim.fingerprint != tool["fingerprint"]
                or claim.tool_call_id != supplied.tool_call_id
                or claim.effect_id != (supplied.operation_id or supplied.command_id)
                or (tool["effect_kind"] == "operation") != (supplied.operation_id is not None)
            ):
                raise PolicyDenied()
            redacted = supplied.model_copy(update={"result": {}, "proposal_id": None})
            if proposal.get("supplied_result") is not None:
                expected = redacted if proposal.get("result_redacted") else supplied
                if proposal["supplied_result"] != expected.model_dump(mode="json"):
                    raise PolicyDenied()
                return str(proposal["result_disposition"])
            if proposal["status"] != "external_started" or tool["state"] != "started":
                raise PolicyDenied()
            run = None
            try:
                run = self._waiting(tx, job, proposal)
                if self.registry is None or self.result_authorizer is None:
                    raise PolicyDenied()
                request = ToolExecution.model_validate(proposal["request"])
                _, arguments, canonical = self.registry.validate(request.call)
                if (
                    request.context != job.grant.context
                    or request.tool_call_id != claim.tool_call_id
                    or execution_fingerprint(request, canonical) != claim.fingerprint
                ):
                    raise PolicyDenied()
                consumer = ResultConsumer(
                    run_id=run_id,
                    context=job.grant.context,
                    conversation_id=job.grant.ref.id,
                    conversation_scope=job.grant.scope,
                    conversation_owner_user_id=job.grant.owner_user_id,
                )
                receipt(
                    self.result_authorizer.check_result(
                        tx, request, arguments, supplied.model_copy(deep=True), consumer=consumer
                    ),
                    consumer,
                )
            except PolicyUnavailable:
                raise AuthorizationUnavailable() from None
            except (PolicyDenied, ToolRejected, AccountUnavailable):
                run = None
            accepted = supplied if run is not None else redacted
            disposition = "queued" if run is not None else "recorded"
            tx.set(
                proposal_path(tenant, proposal_id),
                {
                    **proposal,
                    "status": "confirmed",
                    "supplied_result": accepted.model_dump(mode="json"),
                    "result_redacted": run is None,
                    "result_disposition": disposition,
                },
            )
            tx.set(
                f"tenants/{tenant}/tool_executions/{claim.tool_call_id}",
                {
                    **tool,
                    "state": "finished",
                    "result": accepted.model_dump(mode="json"),
                    "result_redacted": run is None,
                    "authority": {**tool["authority"], "confirmation": "confirmed"}
                    if proposal.get("resume_mode", "execute") == "execute"
                    else tool["authority"],
                },
            )
            if run is not None:
                self._resume(
                    tx,
                    job,
                    run,
                    "outcome_unknown" if supplied.status == "outcome_unknown" else "confirmed",
                )
            elif job.status == "waiting":
                self.jobs._stop(tx, job, "obsolete", "authorization_changed")
            return disposition

        return self.jobs.store.run(work)

    def expire(
        self, tenant: ResourceId, pages: JobPages, *, after: str | None = None, limit: int = 100
    ) -> str | None:
        """Bounded scheduler/recovery entry; no waiting worker or external scheduler required."""
        if not 1 <= limit <= 100:
            raise ValueError("Invalid expiry limit")
        records = pages.page(f"tenants/{tenant}/run_proposals", after=after, limit=limit)
        for _, record in records:
            if (
                record["status"] == "pending"
                and normalize_utc(record["expires_at"]) <= self.jobs.now()
            ):
                try:
                    self.decide(
                        tenant, ResourceId(record["run_id"]), ResourceId(record["id"]), "expired"
                    )
                except PolicyUnavailable:
                    raise AuthorizationUnavailable() from None
                except PolicyDenied:
                    # Expiry with revoked authority has no executable continuation.
                    self._obsolete(tenant, ResourceId(record["run_id"]), ResourceId(record["id"]))
        return records[-1][0].rsplit("/", 1)[-1] if len(records) == limit else None

    def _obsolete(self, tenant: ResourceId, run_id: ResourceId, proposal_id: ResourceId) -> None:
        def work(tx: Transaction) -> None:
            doc = tx.get(job_path(tenant, job_id(tenant, KIND, run_id)))
            proposal = tx.get(proposal_path(tenant, proposal_id))
            if doc is None or proposal is None or proposal["status"] != "pending":
                return
            job = Job.model_validate(doc)
            if job.status != "waiting":
                return
            tx.set(proposal_path(tenant, proposal_id), {**proposal, "status": "expired"})
            self.jobs._stop(tx, job, "obsolete", "authorization_changed")

        self.jobs.store.run(work)

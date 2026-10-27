"""DB-only rejection/expiry composition shared by HTTP and confirmation recovery."""

from collections.abc import Callable

from brainoid_agent_core.types import ToolResult
from brainoid_contracts import ResourceId, normalize_utc
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job, Jobs
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.scopes import PolicyDenied

from brainoid_runs.state import KIND, RunState, run_path


def denied(tool: Document) -> Document:
    return ToolResult(
        tool_call_id=tool["tool_call_id"],
        status="failed",
        result={},
        error_code="rejected",
        operation_id=tool["effect_id"] if tool["effect_kind"] == "operation" else None,
        command_id=tool["effect_id"] if tool["effect_kind"] == "command" else None,
    ).model_dump(mode="json")


def resume(tx: Transaction, jobs: Jobs, job: Job, run: Document, decision: str) -> None:
    tx.set(
        run_path(job.tenant_id, job.operation_id),
        {
            **run,
            "status": "queued",
            "confirmation_decision": decision,
            "version": run["version"] + 1,
        },
    )
    jobs._write(
        tx,
        job,
        status="pending",
        available_at=jobs.now(),
        dispatch_after=jobs.now(),
        dispatch_pending=False,
        dispatch_generation=job.dispatch_generation + 1,
        lease_generation=job.lease_generation + 1,
        lease_expires_at=None,
        attempts=max(0, job.attempts - 1),
    )


def prepare_decision(
    tx: Transaction,
    jobs: Jobs,
    state: RunState,
    record: Document,
    decision: str,
) -> Callable[[], None]:
    """Read all evidence before writes; an early business decision is joined at suspend."""
    tenant = ResourceId(record["tenant_id"])
    identity = ResourceId(record["id"])
    path = f"tenants/{tenant}/run_proposals/{identity}"
    run_id = record.get("run_id")
    if run_id is None:
        return lambda: None
    run = tx.get(run_path(tenant, run_id))
    if run is None or run.get("status") == "cancelled":
        raise PolicyDenied()
    proposal = tx.get(path)
    if proposal is None:
        if run.get("status") not in ("queued", "running"):
            raise PolicyDenied()
        return lambda: None
    document = tx.get(job_path(tenant, job_id(tenant, KIND, ResourceId(run_id))))
    tool_path = f"tenants/{tenant}/tool_executions/{record['tool_call_id']}"
    tool = tx.get(tool_path)
    if document is None or tool is None:
        raise PolicyDenied()
    job = Job.model_validate(document)
    if (
        proposal["run_id"] != run_id
        or proposal["tool_call_id"] != record["tool_call_id"]
        or proposal["checkpoint_version"] != record["checkpoint_version"]
        or proposal["expires_at"] != record["expires_at"]
        or proposal["fingerprint"] != tool["fingerprint"]
        or tool["authority"]["run_id"] != run_id
        or proposal.get("resume_mode") != "result"
    ):
        raise PolicyDenied()
    if proposal["status"] != "pending":
        if proposal["status"] != decision:
            raise PolicyDenied()
        return lambda: None
    run, _, _, _ = state.check(tx, job, waiting=True)
    if (
        run.get("waiting_proposal_id") != identity
        or run.get("checkpoint_version") != record["checkpoint_version"]
        or tool.get("result", {}).get("status") != "waiting_confirmation"
        or tool["result"].get("proposal_id") != identity
        or (decision == "expired" and normalize_utc(record["expires_at"]) > jobs.now())
    ):
        raise PolicyDenied()

    def write() -> None:
        tx.set(path, {**proposal, "status": decision, "decision": decision})
        tx.set(
            tool_path,
            {
                **tool,
                "state": "finished",
                "result": denied(tool),
                "confirmation_result": tool["result"],
            },
        )
        resume(tx, jobs, job, run, decision)

    return write

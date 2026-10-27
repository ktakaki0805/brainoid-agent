"""DB-only Calendar admission; canonical run bindings never come from HTTP input."""

from collections.abc import Callable
from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job, Jobs
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.scopes import PolicyDenied

from brainoid_runs.state import KIND, RunState, run_path


def prepare_admission(
    tx: Transaction, jobs: Jobs, state: RunState, calendar: Document
) -> tuple[Document | None, Callable[[], None]]:
    """Join a suspended run now, or retain a binding for the producer's suspend TX."""
    if calendar.get("run_id") is None:
        return None, lambda: None
    tenant, run_id, identity = (ResourceId(calendar[k]) for k in ("tenant_id", "run_id", "id"))
    run = tx.get(run_path(tenant, run_id))
    saved = tx.get(job_path(tenant, job_id(tenant, KIND, run_id)))
    path = f"tenants/{tenant}/run_proposals/{identity}"
    proposal = tx.get(path)
    tool_path = f"tenants/{tenant}/tool_executions/{calendar['tool_call_id']}"
    tool = tx.get(tool_path)
    if (
        run is None
        or saved is None
        or tool is None
        or run.get("status") not in ("queued", "running", "waiting_confirmation")
        or tool.get("name") != "calendar.propose"
        or tool.get("effect_kind") != "operation"
        or tool.get("effect_id") != identity
        or tool.get("authority", {}).get("run_id") != run_id
        or tool["tool_call_id"] not in run.get("tool_call_ids", [])
    ):
        raise PolicyDenied()
    job = Job.model_validate(saved)
    state.check(tx, job, waiting=proposal is not None, check_results=False)
    claim = {
        "id": str(uuid5(UUID(identity), "calendar-external-claim")),
        "tenant_id": tenant,
        "run_id": run_id,
        "proposal_id": identity,
        "tool_call_id": tool["tool_call_id"],
        "effect_id": identity,
        "fingerprint": tool["fingerprint"],
    }
    if proposal is None:
        if calendar["checkpoint_version"] != run.get("checkpoint_version", 0) + 1:
            raise PolicyDenied()
        return claim, lambda: None
    if (
        proposal.get("status") != "pending"
        or proposal.get("resume_mode") != "result"
        or proposal.get("run_id") != run_id
        or proposal.get("tool_call_id") != tool["tool_call_id"]
        or proposal.get("fingerprint") != tool["fingerprint"]
        or proposal.get("checkpoint_version") != calendar["checkpoint_version"]
        or proposal.get("expires_at") != calendar["expires_at"]
        or run.get("waiting_proposal_id") != identity
        or tool.get("result", {}).get("status") != "waiting_confirmation"
        or tool["result"].get("proposal_id") != identity
    ):
        raise PolicyDenied()

    def write() -> None:
        tx.set(
            path,
            {
                **proposal,
                "status": "external_started",
                "decision": "confirmed",
                "external_claim": claim,
                "external_started_at": jobs.now().isoformat(),
            },
        )
        tx.set(
            tool_path,
            {
                **tool,
                "state": "started",
                "result": None,
                "confirmation_result": tool["result"],
                "external_claim": claim,
            },
        )

    return claim, write

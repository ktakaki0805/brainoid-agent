"""DB-only cancellation; it fences future work and never rolls back tool effects."""

from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import fenced_document, source_write_reserved
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import Job, Jobs
from brainoid_jobs.models import job_id
from brainoid_jobs.repository import path as job_path
from brainoid_policy.scopes import PolicyDenied
from brainoid_usage import UsageLedger

from brainoid_runs import events
from brainoid_runs.state import KIND, conversation_path, run_path


def cancellation(tx: Transaction, jobs: Jobs, run: Document) -> Document:
    tenant, identity = ResourceId(run["tenant_id"]), ResourceId(run["run_id"])
    if source_write_reserved(tx, tenant, conversation_path(tenant, run["conversation_id"])):
        raise PolicyDenied()
    doc = tx.get(job_path(tenant, job_id(tenant, KIND, identity)))
    parent = tx.get(conversation_path(tenant, run["conversation_id"]))
    if doc is None or parent is None:
        raise PolicyDenied()
    parent = fenced_document(
        tx, tenant, "conversations", ResourceId(run["conversation_id"]), parent
    )
    if parent.get("deleted") is not False or parent.get("deletion_epoch") != 0:
        raise PolicyDenied()
    job = Job.model_validate(doc)
    proposal_path = None
    proposal = None
    if run.get("waiting_proposal_id") is not None:
        proposal_path = f"tenants/{tenant}/run_proposals/{run['waiting_proposal_id']}"
        proposal = tx.get(proposal_path)
    # Read every execution, including a started operation with no result yet.
    executions = [
        tx.get(f"tenants/{tenant}/tool_executions/{call}") for call in run.get("tool_call_ids", [])
    ]
    calendars = []
    for execution in executions:
        if execution is not None and execution.get("name") == "calendar.propose":
            path = f"tenants/{tenant}/calendar_proposals/{execution['effect_id']}"
            canonical = tx.get(path)
            if (
                canonical is not None
                and canonical.get("run_id") == identity
                and canonical.get("status") in ("pending", "confirmed")
            ):
                calendars.append((path, canonical))
    may_have_completed = any(
        e.get("authority", {}).get("run_id") == identity
        and (
            (
                e.get("state") == "started"
                and (e.get("name") != "calendar.propose" or e.get("external_claim") is not None)
            )
            or (
                e.get("result") is not None
                and e["result"].get("status") not in ("waiting_confirmation", "failed")
            )
        )
        for e in executions
        if e is not None
    )
    if run["status"] in ("completed", "failed", "cancelled"):
        return run
    release = UsageLedger(jobs.store, now=jobs.now).prepare_release(tx, tenant, identity)
    release()
    for path, canonical in calendars:
        tx.set(path, {**canonical, "status": "cancelled", "version": canonical["version"] + 1})
    updated = {**run, "status": "cancelled", "version": run["version"] + 1}
    updated.pop("waiting_proposal_id", None)
    events.append(
        tx,
        updated,
        events.Cancelled(
            run_id=identity,
            seq=run.get("last_event_seq", 0) + 1,
            occurred_at=jobs.now(),
            payload=events.CancelledPayload(external_action_may_have_completed=may_have_completed),
        ),
    )
    if proposal is not None and proposal_path is not None and proposal["status"] == "pending":
        tx.set(proposal_path, {**proposal, "status": "cancelled"})
    jobs._write(
        tx,
        job,
        status="obsolete",
        reason="cancelled",
        lease_expires_at=None,
        lease_generation=job.lease_generation + 1,
        dispatch_pending=False,
        dispatch_generation=job.dispatch_generation + 1,
    )
    if parent.get("active_run_id") == identity:
        tx.set(
            conversation_path(tenant, run["conversation_id"]),
            {
                **parent,
                "active_run_id": None,
                "version": parent["version"] + 1,
                "history_revision": parent.get("history_revision", 0) + 1,
            },
        )
    tx.set(run_path(tenant, identity), updated)
    return updated

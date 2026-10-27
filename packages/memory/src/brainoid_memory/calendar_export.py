"""Transaction-ordered Calendar erasure boundary, isolated by tenant and owner."""

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction
from brainoid_jobs import AuthorizationUnavailable
from pydantic import TypeAdapter

from brainoid_memory.deletion_ledger import DeletionIntent


class CalendarCleanupPending(AuthorizationUnavailable):
    """No authority to copy until accepted erasures finish; refund the job attempt."""


def barrier_path(tenant: ResourceId, subject: ResourceId) -> str:
    return f"tenants/{tenant}/calendar_export_barriers/{subject}"


def state(tx: Transaction, tenant: ResourceId, subject: ResourceId) -> Document:
    record = tx.get(barrier_path(tenant, subject))
    if record is None:
        return {"epoch": 0, "pending": {}, "completed": 0}
    value, pending = record.get("epoch"), record.get("pending")
    if type(value) is not int or value < 0:
        raise ValueError("Invalid Calendar export barrier")
    # A legacy nonzero barrier does not prove completion. Restore its exact
    # pending set from accepted reservations/receipts before enabling exports.
    if pending is None and value == 0:
        pending = {}
    if not isinstance(pending, dict):
        raise ValueError("Calendar pending erasures must be restored")
    completed = record.get("completed", 0 if value == 0 else None)
    if type(completed) is not int or completed < 0 or completed + len(pending) != value:
        raise ValueError("Calendar pending/completed counts must be restored")
    for operation, admission in pending.items():
        TypeAdapter(ResourceId).validate_python(operation)
        if type(admission) is not int or not 1 <= admission <= value:
            raise ValueError("Invalid Calendar pending erasure")
    if len(set(pending.values())) != len(pending):
        raise ValueError("Duplicate Calendar pending epoch")
    return {"epoch": value, "pending": dict(pending), "completed": completed}


def epoch(tx: Transaction, tenant: ResourceId, subject: ResourceId) -> int:
    return int(state(tx, tenant, subject)["epoch"])


def ensure_ready(tx: Transaction, tenant: ResourceId, subject: ResourceId) -> None:
    if state(tx, tenant, subject)["pending"]:
        raise CalendarCleanupPending()


def checked_stop(tx: Transaction, intent: DeletionIntent) -> tuple[Document, Document]:
    scope = intent.scopes[0]
    assert scope.owner_subject_id is not None and scope.export_epoch is not None
    barrier = state(tx, intent.tenant_id, scope.owner_subject_id)
    reserved = tx.get(
        f"tenants/{intent.tenant_id}/calendar_disconnect_requests/{intent.operation_id}"
    )
    if barrier["epoch"] < scope.export_epoch:
        raise ValueError("Calendar export barrier must be restored")
    if reserved is None or reserved.get("intent") != intent.model_dump(mode="json"):
        raise ValueError("Calendar stop reservation must be restored")
    pending = barrier["pending"].get(intent.operation_id)
    complete = reserved.get("export_cleanup_complete")
    if not (
        (pending == scope.export_epoch and complete is None)
        or (pending is None and complete == scope.export_epoch)
    ):
        raise ValueError("Calendar pending erasure must be restored")
    return barrier, reserved


def completion(tx: Transaction, intent: DeletionIntent) -> tuple[tuple[str, Document], ...]:
    """Prepare writes ONLY after PurgeResource has verified all physical receipts."""
    barrier, reserved = checked_stop(tx, intent)
    if intent.operation_id not in barrier["pending"]:
        return ()
    scope = intent.scopes[0]
    assert scope.owner_subject_id is not None
    pending = {
        key: value for key, value in barrier["pending"].items() if key != intent.operation_id
    }
    return (
        (
            barrier_path(intent.tenant_id, scope.owner_subject_id),
            {**barrier, "pending": pending, "completed": barrier["completed"] + 1},
        ),
        (
            f"tenants/{intent.tenant_id}/calendar_disconnect_requests/{intent.operation_id}",
            {**reserved, "export_cleanup_complete": scope.export_epoch},
        ),
    )


def admitted_epoch(registry: Document) -> int:
    # Pre-CAL05 registries were admitted before the first Calendar stop barrier.
    # All export writers must understand this field before enabling disconnect.
    value = registry.get("calendar_epoch", 0)
    if type(value) is not int or value < 0:
        raise ValueError("Invalid Calendar export admission")
    return value

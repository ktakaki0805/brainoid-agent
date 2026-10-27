"""Closed Calendar provenance selection for the existing bounded lifecycle walker."""

from brainoid_contracts import ResourceId
from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.forget import operation_path

from brainoid_worker.purge_handlers import VECTOR, Decision, LegacyProvenanceMissing

ROOTS = frozenset(
    {
        "integrations",
        "calendar_authorizations",
        "calendar_proposals",
        "runs",
        "jobs",
        "tool_executions",
        "run_proposals",
        "conversations",
        "facts",
        "notes",
        "tasks",
        "memory_effects",
        "note_effects",
        "task_effects",
        "task_events",
        "reminders",
        "reminder_schedules",
        "notifications",
        "caches",
        "exports",
    }
)


def selection_path(intent: DeletionIntent, path: str) -> str:
    return f"tenants/{intent.tenant_id}/lifecycle_selections/{fingerprint([intent.operation_id, path])}"


def message_marker(intent: DeletionIntent, identity: str) -> str:
    return (
        operation_path(intent.tenant_id, intent.operation_id)
        + "/purge_calendar_messages/"
        + identity
    )


def selected(
    tx: Transaction,
    path: str,
    doc: Document | None,
    intent: DeletionIntent,
    seen: frozenset[str] = frozenset(),
) -> bool:
    parts = path.split("/")
    if len(parts) < 4 or parts[:2] != ["tenants", str(intent.tenant_id)]:
        return False
    if path in seen:
        return False
    if len(seen) >= 64:
        raise LegacyProvenanceMissing()
    seen = seen | {path}
    if tx.get(selection_path(intent, path)) == {
        "operation_id": intent.operation_id,
        "path": path,
        "request_hash": intent.request_hash,
    }:
        return True
    root, scope = parts[2], intent.scopes[0]
    if root not in ROOTS and VECTOR.fullmatch(root) is None:
        return False
    if (
        len(parts) == 4
        and doc is None
        and (
            (root == "integrations" and parts[3] == scope.resource_id)
            or (root == "calendar_authorizations" and parts[3] == scope.authorization_id)
        )
    ):
        # These exact producer IDs are part of the independently accepted intent.
        return True
    if doc is None:
        return False
    if doc.get("tenant_id", intent.tenant_id) != intent.tenant_id:
        raise ValueError("Calendar derived tenant mismatch")

    def linked(collection: str, identity: object) -> bool:
        if not isinstance(identity, str) or not identity or "/" in identity:
            return False
        location = f"tenants/{intent.tenant_id}/{collection}/{identity}"
        return selected(tx, location, tx.get(location), intent, seen)

    def sources(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        return any(
            isinstance(source, dict) and source.get("integration_id") == scope.resource_id
            for source in value.get("calendar_sources", [])
        )

    if root in {"integrations", "calendar_authorizations"}:
        target = scope.resource_id if root == "integrations" else scope.authorization_id
        return (
            len(parts) == 4
            and parts[3] == target
            and isinstance(doc.get("owner_user_id"), str)
            and subject_id(doc["owner_user_id"]) == scope.owner_subject_id
            and (root != "integrations" or doc.get("provider") == "google_calendar")
        )
    if root == "conversations" and len(parts) == 4:
        return False
    if root == "jobs":
        if doc.get("kind") in {"memory_purge", "lifecycle_purge"}:
            return False
        if doc.get("kind") == "calendar_write":
            return linked("calendar_proposals", doc.get("operation_id"))
        if doc.get("kind") == "run_turn":
            return linked("runs", doc.get("operation_id"))
        ref = doc.get("grant", {}).get("resource", {}).get("ref", {})
        return (
            ref.get("tenant_id") == intent.tenant_id
            and ref.get("kind") in ROOTS
            and linked(ref["kind"], ref.get("id"))
        )
    if VECTOR.fullmatch(root):
        return linked("facts", parts[3])
    if root == "calendar_proposals" and doc.get("integration_id") == scope.resource_id:
        return (
            isinstance(doc.get("owner_user_id"), str)
            and subject_id(doc["owner_user_id"]) == scope.owner_subject_id
        )
    if (
        root == "conversations"
        and len(parts) == 6
        and parts[4] == "messages"
        and doc.get("role") == "user"
    ):
        return False
    if sources(doc) or sources(doc.get("origin", {})):
        return True
    if root in {"tool_executions", "run_proposals"} and linked(
        "runs", doc.get("run_id", doc.get("authority", {}).get("run_id"))
    ):
        return True
    if root == "conversations" and len(parts) == 6:
        if parts[4] == "messages" and linked("runs", doc.get("run_id")):
            return True
        if parts[4] == "summaries":
            source = doc.get("sources", {})
            return any(
                isinstance(item, dict)
                and item.get("included") is True
                and tx.get(message_marker(intent, item.get("resource_id", "missing"))) is not None
                for item in source.get("messages", [])
            )
    for source in doc.get("source_refs", []):
        if not isinstance(source, dict):
            continue
        identity = source.get("resource_id")
        if isinstance(identity, str) and "/" not in identity:
            if source.get("kind") == "message":
                marker = tx.get(message_marker(intent, identity))
                if (
                    marker is not None
                    and type(source.get("version")) is int
                    and source["version"] <= marker["version"]
                ):
                    return True
            if source.get("kind") == "note" and linked("notes", identity):
                return True
    if any(
        isinstance(item, dict) and linked("facts", item.get("memory_id"))
        for item in doc.get("memory_records", [])
    ):
        return True
    if root in {"memory_effects", "note_effects", "task_effects", "task_events"}:
        collection = {
            "memory_effects": "facts",
            "note_effects": "notes",
            "task_effects": "tasks",
            "task_events": "tasks",
        }[root]
        return any(
            isinstance(item, dict) and linked(collection, item.get("id"))
            for item in (doc.get("record"), doc.get("previous"))
        )
    if root in {"reminders", "reminder_schedules", "notifications"}:
        return linked("tasks", doc.get("task_id")) or linked("reminders", doc.get("reminder_id"))
    # Content-free export metadata survives; the independent registry hook
    # erases only this owner's artifacts admitted before the disconnect.
    return False


def bound(tx: Transaction, path: str, intent: DeletionIntent, force: bool) -> bool:
    parts = path.split("/")
    if parts[:2] != ["tenants", str(intent.tenant_id)]:
        raise ValueError("Unaccepted Calendar tenant")
    if len(parts) <= 3:
        return True
    # Conversations are containers, never selected wholesale by a Calendar grant.
    depth = 6 if parts[2] == "conversations" else 4
    if len(parts) <= depth and not force:
        return True
    ancestor = "/".join(parts[:depth])
    return selected(tx, ancestor, tx.get(ancestor), intent)


def decide(tx: Transaction, path: str, doc: Document | None, intent: DeletionIntent) -> Decision:
    parts = path.split("/")
    if len(parts) >= 3 and parts[2] not in ROOTS and VECTOR.fullmatch(parts[2]) is None:
        return Decision()
    if len(parts) == 2 or (len(parts) == 4 and parts[2] == "conversations"):
        return Decision(children=True)
    chosen = selected(tx, path, doc, intent)
    if chosen and len(parts) == 4 and parts[2] == "integrations" and doc is not None:
        retained = {
            key: value
            for key, value in doc.items()
            if key
            in {
                "id",
                "tenant_id",
                "version",
                "created_at",
                "updated_at",
                "provider",
                "status",
                "owner_user_id",
                "visibility",
                "display_policy",
                "binding",
                "deleted",
                "deletion_epoch",
                "stop_reason",
                "reconnect_allowed",
                "cleanup_status",
                "provider_revocation",
                "provider_revocation_scope",
                "disconnect_operation_id",
            }
        }
        return Decision(
            replacement=retained if retained != doc else None, children=True, selected=True
        )
    markers: tuple[tuple[str, Document], ...] = ()
    if chosen and len(parts) == 4 and parts[2] == "calendar_proposals" and doc is not None:
        location = operation_path(intent.tenant_id, ResourceId(parts[3]))
        operation = tx.get(location)
        if (
            operation is not None
            and operation.get("operation", {}).get("kind") == "calendar_write"
            and operation["operation"]["status"] in {"pending", "running"}
        ):
            started = operation.get("started_at") is not None
            markers = (
                (
                    location,
                    {
                        **operation,
                        "operation": {
                            **operation["operation"],
                            "status": "outcome_unknown" if started else "failed",
                            "error_code": "outcome_unknown" if started else "permission_denied",
                            "updated_at": intent.created_at.isoformat(),
                        },
                    },
                ),
            )
    if (
        chosen
        and len(parts) == 6
        and parts[2] == "conversations"
        and parts[4] == "messages"
        and doc is not None
    ):
        markers = (
            (
                message_marker(intent, parts[5]),
                {"version": doc["version"], "conversation_id": parts[3]},
            ),
        )
    return Decision(
        erase=chosen and doc is not None,
        selected=chosen,
        children=chosen or doc is None,
        deletion_markers=markers,
    )

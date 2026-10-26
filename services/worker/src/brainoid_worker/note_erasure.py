"""Narrow source-to-Fact erasure proof; never a user read grant."""

from brainoid_contracts import ResourceId
from brainoid_contracts.deletion import tombstone_path
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import DeletionIntent, DeletionTarget
from brainoid_memory.forget import operation_path


def note_targets(intent: DeletionIntent) -> tuple[DeletionTarget, ...]:
    """Only sources in the accepted intent; never infer additional Fact authority."""
    if intent.kind not in {"forget_memory", "delete_resource"}:
        return ()
    return tuple(target for target in intent.targets if target.kind == "note")


def note_copy(document: Document, intent: DeletionIntent) -> bool:
    return document.get("tenant_id") == intent.tenant_id and any(
        document.get("id") == target.resource_id
        and type(document.get("version")) is int
        and 1 <= document["version"] <= target.version
        for target in note_targets(intent)
    )


def sourced(document: Document, intent: DeletionIntent) -> bool:
    if document.get("tenant_id") != intent.tenant_id:
        return False
    refs = document.get("source_refs", [])
    return isinstance(refs, list) and any(
        isinstance(ref, dict)
        and ref.get("kind") == "note"
        and ref.get("resource_id") == target.resource_id
        and type(ref.get("version")) is int
        and 1 <= ref["version"] <= target.version
        for ref in refs
        for target in note_targets(intent)
    )


def fact_selected(tx: Transaction, identity: str, intent: DeletionIntent) -> bool:
    if intent.kind == "forget_memory":
        return identity == intent.targets[0].resource_id
    if intent.kind != "delete_resource":
        return False
    doc = tx.get(f"tenants/{intent.tenant_id}/facts/{identity}")
    if doc is not None and doc.get("id") == identity and sourced(doc, intent):
        return True
    return tx.get(selection_path(intent, identity)) == selection(intent, identity)


def selection_path(intent: DeletionIntent, identity: str) -> str:
    return operation_path(intent.tenant_id, intent.operation_id) + "/purge_facts/" + identity


def selection(intent: DeletionIntent, identity: str) -> Document:
    return {
        "fact_id": identity,
        "request_hash": intent.request_hash,
        "source": intent.targets[0].model_dump(mode="json"),
    }


def fact_markers(
    tx: Transaction, identity: str, document: Document, intent: DeletionIntent
) -> tuple[tuple[str, Document], ...]:
    if document.get("id") != identity or not sourced(document, intent):
        raise ValueError("Unproved derived fact")
    version, epoch = document.get("version"), document.get("deletion_epoch")
    if type(version) is not int or version < 1 or type(epoch) is not int or epoch < 0:
        raise ValueError("Invalid derived fact metadata")
    marker = {
        "operation_id": intent.operation_id,
        "mode": "delete",
        "created_at": intent.created_at.isoformat(),
        "target": {
            "kind": "fact",
            "resource_id": identity,
            "parent_id": None,
            "version": version,
            "deletion_epoch": epoch + 1,
        },
        "derived_source": intent.targets[0].model_dump(mode="json"),
    }
    path = tombstone_path(intent.tenant_id, "facts", ResourceId(identity))
    # An overlapping accepted Fact forget owns its immutable fence. Retain it;
    # this source deletion keeps independent, content-free selection evidence.
    previous = tx.get(path)
    return (
        (selection_path(intent, identity), selection(intent, identity)),
        *(() if previous is not None else ((path, marker),)),
    )


def task_copy(document: Document, intent: DeletionIntent) -> bool:
    return (
        intent.kind == "delete_resource"
        and document.get("tenant_id") == intent.tenant_id
        and any(
            target.kind == "task"
            and document.get("id") == target.resource_id
            and type(document.get("version")) is int
            and 1 <= document["version"] <= target.version
            for target in intent.targets
        )
    )

"""Persistent, content-free deletion fences shared by model and export readers."""

from uuid import UUID, uuid5

from brainoid_contracts import ResourceId
from brainoid_contracts.transactions import Document, Transaction


def tombstone_path(
    tenant: ResourceId, kind: str, identity: ResourceId, parent: ResourceId | None = None
) -> str:
    key = uuid5(UUID(tenant), f"deletion:{kind}:{parent}:{identity}")
    return f"tenants/{tenant}/deletion_tombstones/{key}"


def fenced_document(
    tx: Transaction,
    tenant: ResourceId,
    kind: str,
    identity: ResourceId,
    document: Document,
    *,
    parent: ResourceId | None = None,
) -> Document:
    """Unknown fence modes deny all use. Suppressed sources remain UI-readable."""
    marker = tx.get(tombstone_path(tenant, kind, identity, parent))
    if marker is None:
        return document
    target = marker.get("target")
    retained = (
        kind in ("messages", "notes")
        and marker.get("mode") == "suppress_reuse"
        and isinstance(target, dict)
        and target.get("kind")
        == {"facts": "fact", "messages": "message", "notes": "note"}.get(kind)
        and target.get("resource_id") == identity
        and target.get("parent_id") == parent
    )
    return {
        **document,
        "suppress_reuse": True,
        "deleted": document.get("deleted") is not False or not retained,
    }


def exportable(
    tx: Transaction,
    tenant: ResourceId,
    kind: str,
    identity: ResourceId,
    document: Document,
    *,
    parent: ResourceId | None = None,
) -> bool:
    """Must accompany current export scope/ownership checks; no authorization grant."""
    current = fenced_document(tx, tenant, kind, identity, document, parent=parent)
    return (
        current.get("deleted") is False
        and type(current.get("deletion_epoch")) is int
        and current["deletion_epoch"] == 0
    )

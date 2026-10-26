"""Persistent, content-free deletion fences shared by model and export readers."""

from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.access import fingerprint
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
    canonical = (
        f"tenants/{tenant}/{kind}/{identity}"
        if parent is None
        else f"tenants/{tenant}/conversations/{parent}/{kind}/{identity}"
    )
    if tx.get(f"resource_deletion_fences/{fingerprint(canonical)}") is not None or lifecycle_denied(
        tx, tenant, document
    ):
        return {**document, "deleted": True, "suppress_reuse": True}
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


def purge_barrier_path(tenant: ResourceId) -> str:
    return f"tenants/{tenant}/purge_barriers/history"


def purge_epoch(tx: Transaction, tenant: ResourceId) -> int:
    """Admission ordering only; never an authorization grant or wall-clock fence."""
    document = tx.get(purge_barrier_path(tenant))
    epoch = 0 if document is None else document.get("epoch")
    if type(epoch) is not int or epoch < 0:
        raise ValueError("Invalid purge admission epoch")
    return epoch


def subject_id(user: FirebaseUid | str) -> ResourceId:
    """Opaque, path-safe identifier; no raw Firebase UID leaves the application DB."""
    return ResourceId(str(uuid5(UUID("7558f16e-29c6-44e5-8d70-5740068da970"), user)))


def lifecycle_account_path(user: FirebaseUid | str) -> str:
    return f"account_deletion_fences/{subject_id(user)}"


def lifecycle_tenant_path(tenant: ResourceId | str) -> str:
    return f"tenant_deletion_fences/{tenant}"


def lifecycle_member_path(tenant: ResourceId | str, user: FirebaseUid | str) -> str:
    return f"tenants/{tenant}/membership_deletion_fences/{subject_id(user)}"


def lifecycle_denied(tx: Transaction, tenant: ResourceId, document: Document) -> bool:
    if tx.get(lifecycle_tenant_path(tenant)) is not None:
        return True
    owner = document.get("owner_user_id")
    return bool(
        document.get("visibility") == "private"
        and isinstance(owner, str)
        and (
            tx.get(lifecycle_account_path(owner)) is not None
            or membership_deletion_pending(tx, tenant, owner)
        )
    )


def membership_deletion_pending(
    tx: Transaction, tenant: ResourceId | str, user: FirebaseUid | str
) -> bool:
    fence = tx.get(lifecycle_member_path(tenant, user))
    if fence is None:
        return False
    operation = fence.get("operation_id")
    saved = tx.get(f"tenants/{tenant}/operations/{operation}")
    if saved is None or saved.get("operation", {}).get("status") != "succeeded":
        return True
    members = tx.find(f"tenants/{tenant}/memberships", "user_id", user)
    return not (
        len(members) == 1
        and members[0].get("authorization_version", 0)
        > fence.get("scope", {}).get("version", 0) + 1
        and members[0].get("status") == "active"
    )

"""Resolve one tenant's destructive scope from current canonical membership metadata."""

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import lifecycle_tenant_path, subject_id
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import LifecycleScope
from brainoid_policy.models import Role
from brainoid_policy.transactions import TransactionPolicyState

from brainoid_api.errors import APIError, ErrorCode


def resolve_tenant(
    tx: Transaction,
    state: TransactionPolicyState,
    tenant: ResourceId,
    user: FirebaseUid,
    *,
    delete: bool,
    confirmation: str | None = None,
) -> tuple[LifecycleScope, dict[str, Document]]:
    if tx.get(lifecycle_tenant_path(tenant)) is not None:
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    current = tx.get(f"tenants/{tenant}")
    member = state.membership(tenant, user)
    if (
        current is None
        or current.get("status") != "active"
        or member is None
        or member.status != "active"
    ):
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    if current.get("membership_mutation") is not None:
        raise APIError(ErrorCode.CONFLICT)
    if delete and (member.role != Role.OWNER or confirmation != current.get("name")):
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    if not delete:
        require_surviving_owner(tx, state, tenant, user)
    matches = tx.query(f"tenants/{tenant}/memberships", {"user_id": user})
    if len(matches) != 1:
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    path, document = matches[0]
    return LifecycleScope(
        kind="tenant" if delete else "member",
        resource_id=tenant if delete else subject_id(user),
        tenant_id=tenant,
        version=current["policy_version"] if delete else member.authorization_version,
        deletion_epoch=current.get("deletion_epoch", 0) + 1 if delete else 1,
    ), {f"tenants/{tenant}": current, path: document}


def require_surviving_owner(
    tx: Transaction, state: TransactionPolicyState, tenant: ResourceId, user: FirebaseUid
) -> None:
    for _, document in tx.collection(f"tenants/{tenant}/memberships"):
        if document.get("user_id") == user:
            continue
        member = state.membership(tenant, FirebaseUid(document["user_id"]))
        if member is not None and member.status == "active" and member.role == Role.OWNER:
            return
    raise APIError(ErrorCode.OWNERSHIP_TRANSFER_REQUIRED)

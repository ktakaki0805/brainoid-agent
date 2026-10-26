"""Resolve the authenticated account across all memberships, never a supplied UID."""

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import lifecycle_tenant_path, subject_id
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import LifecycleScope
from brainoid_policy.transactions import TransactionPolicyState

from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.services.tenant_deletion import resolve_tenant


def resolve_account(
    tx: Transaction, state: TransactionPolicyState, user: FirebaseUid
) -> tuple[tuple[LifecycleScope, ...], dict[str, Document]]:
    profiles = tx.query("users", {"user_id": user})
    if len(profiles) > 1 or (profiles and profiles[0][1].get("status") != "active"):
        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
    documents = dict(profiles)
    scopes = [
        LifecycleScope(
            kind="account",
            resource_id=subject_id(user),
            version=profiles[0][1]["version"] if profiles else 1,
        )
    ]
    memberships = tx.memberships(user)
    tenants: set[str] = set()
    for path, document in memberships:
        parts = path.split("/")
        if (
            len(parts) != 4
            or parts[0] != "tenants"
            or parts[2] != "memberships"
            or document.get("tenant_id") != parts[1]
            or parts[1] in tenants
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        tenants.add(parts[1])
        tenant = ResourceId(parts[1])
        destroying = tx.get(lifecycle_tenant_path(tenant))
        if destroying is not None:
            # The tenant's permanent fence already prevents every new membership or
            # content write. Its accepted intent supplies independent erasure authority.
            if (
                destroying.get("mode") != "delete"
                or destroying.get("scope", {}).get("resource_id") != tenant
            ):
                raise APIError(ErrorCode.CONFLICT)
            scopes.append(
                LifecycleScope(
                    kind="member",
                    resource_id=subject_id(user),
                    tenant_id=tenant,
                    version=document["authorization_version"],
                )
            )
            continue
        if document.get("status") == "active":
            scope, snapshot = resolve_tenant(tx, state, tenant, user, delete=False)
            scopes.append(scope)
            documents.update(snapshot)
        else:
            # Inactive membership still owns private data requiring erasure. Serialize
            # with any ongoing transfer/grant before locking this complete inventory.
            current = tx.get(f"tenants/{tenant}")
            if (
                current is None
                or current.get("status") != "active"
                or current.get("membership_mutation") is not None
            ):
                raise APIError(ErrorCode.CONFLICT)
            scopes.append(
                LifecycleScope(
                    kind="member",
                    resource_id=subject_id(user),
                    tenant_id=tenant,
                    version=document["authorization_version"],
                )
            )
            documents.update({f"tenants/{tenant}": current, path: document})
    return tuple([scopes[0], *sorted(scopes[1:], key=lambda item: str(item.tenant_id))]), documents

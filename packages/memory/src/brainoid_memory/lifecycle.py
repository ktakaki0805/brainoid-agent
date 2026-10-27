"""Exact lifecycle deletion bindings shared by acceptance, policy and purge."""

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    UserActorContext,
    Visibility,
)
from brainoid_contracts.transactions import Document
from brainoid_policy.models import Action, ResourceKind, ResourceRef, ResourceState, UserSubject
from brainoid_policy.scopes import Principal, ResourceGrant

from brainoid_memory.deletion_ledger import DeletionIntent, LifecycleScope

KIND = "lifecycle_purge"


def scope_fence(scope: LifecycleScope) -> str:
    if scope.kind == "integration":
        return f"tenants/{scope.tenant_id}/integration_deletion_fences/{scope.resource_id}"
    if scope.kind == "account":
        return f"account_deletion_fences/{scope.resource_id}"
    if scope.kind == "tenant":
        return f"tenant_deletion_fences/{scope.resource_id}"
    return f"tenants/{scope.tenant_id}/membership_deletion_fences/{scope.resource_id}"


def audit_grant(tenant: ResourceId, operation: ResourceId, user: str) -> ResourceGrant:
    """Content-free audit envelope, deliberately not a grant to a canonical resource.

    Only the dedicated executor interprets accepted lifecycle scopes. Ordinary Jobs
    cannot resolve this synthetic reference and therefore cannot execute it.
    """
    subject = UserSubject(user_id=FirebaseUid(user))
    return ResourceGrant(
        principal=Principal(
            context=TenantContext(
                tenant_id=tenant,
                actor=UserActorContext(
                    actor_id=FirebaseUid(user), allowed_scopes=frozenset(), authorization_version=1
                ),
            ),
            subject=subject,
            stamp="lifecycle-audit-only",
            user_id=subject.user_id,
            role=None,
            membership=None,
        ),
        resource=ResourceState(
            ref=ResourceRef(tenant_id=tenant, kind=ResourceKind.CONVERSATION, id=operation),
            scope=Scope(
                visibility=Visibility.PRIVATE, display_policy=DisplayPolicy.AUTHENTICATED_SESSION
            ),
            owner_user_id=subject.user_id,
            created_by=user,
            version=1,
            deletion_epoch=0,
        ),
        action=Action.WRITE,
    )


def marker(intent: DeletionIntent, scope: LifecycleScope) -> Document:
    return {
        "operation_id": intent.operation_id,
        "scope": scope.model_dump(mode="json"),
        "mode": "delete",
        "request_hash": intent.request_hash,
    }

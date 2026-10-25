"""Narrow search filters and immutable receipts, not transferable authorization tokens."""

from dataclasses import dataclass

from brainoid_contracts import (
    DisplayPolicy,
    FirebaseUid,
    ResourceId,
    Scope,
    TenantContext,
    Visibility,
)

from brainoid_policy.models import Membership, ResourceRef, ResourceState, Role, Subject


class PolicyDenied(Exception):
    """Content-free denial. Hidden resources always share the same public 404 code."""

    def __init__(self, *, hidden: bool = True) -> None:
        self.code = "resource_not_found" if hidden else "operation_forbidden"
        super().__init__(self.code)


@dataclass(frozen=True)
class Principal:
    context: TenantContext
    subject: Subject
    stamp: str
    user_id: FirebaseUid | None
    role: Role | None
    membership: Membership | None


@dataclass(frozen=True)
class SearchFilter:
    """Apply BEFORE retrieval/counts/graph traversal; query each filter separately."""

    visibility: Visibility
    owner_user_id: FirebaseUid | None
    display_policies: frozenset[DisplayPolicy]
    room_ids: frozenset[ResourceId] | None = None
    entity_ids: frozenset[ResourceId] | None = None

    def matches(self, resource: ResourceState) -> bool:
        return (
            resource.scope.visibility == self.visibility
            and resource.scope.display_policy in self.display_policies
            and (
                self.visibility == Visibility.SHARED or resource.owner_user_id == self.owner_user_id
            )
            and (
                resource.room_id is None
                or self.room_ids is None
                or resource.room_id in self.room_ids
            )
            and (
                resource.entity_id is None
                or self.entity_ids is None
                or resource.entity_id in self.entity_ids
            )
        )


@dataclass(frozen=True)
class SearchScope:
    principal: Principal
    filters: tuple[SearchFilter, ...]
    kind: str
    output_stamp: str | None


@dataclass(frozen=True)
class ResourceGrant:
    principal: Principal
    resource: ResourceState
    action: str
    output_stamp: str | None = None

    @property
    def context(self) -> TenantContext:
        return self.principal.context

    @property
    def scope(self) -> Scope:
        return self.resource.scope

    @property
    def owner_user_id(self) -> FirebaseUid | None:
        return self.resource.owner_user_id

    @property
    def ref(self) -> ResourceRef:
        return self.resource.ref

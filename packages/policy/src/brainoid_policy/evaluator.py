"""Read current authority on EVERY entry; no role, context or grant supplied by the model."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime

from brainoid_contracts import (
    DeviceActorContext,
    DisplayPolicy,
    FirebaseUid,
    PrivateSessionContext,
    ResourceId,
    TenantContext,
    UserActorContext,
    Visibility,
    normalize_utc,
)
from pydantic import BaseModel

from brainoid_policy.models import (
    Action,
    DeviceState,
    DeviceSubject,
    ManagementAction,
    Membership,
    ResourceKind,
    ResourceRef,
    ResourceState,
    Role,
    SessionState,
    StateReader,
    Subject,
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, Principal, ResourceGrant, SearchFilter, SearchScope

USER_SCOPES = frozenset(
    {"shared", "conversation", "memory.read", "memory.write", "notes", "tasks", "reminders"}
)
ALL_DISPLAY = frozenset(DisplayPolicy)


def _intersect(
    first: frozenset[ResourceId] | None,
    second: frozenset[ResourceId] | None,
) -> frozenset[ResourceId] | None:
    if first is None:
        return second
    return first if second is None else first & second


def _stamp(*records: BaseModel) -> str:
    values = []
    for record in records:
        data = record.model_dump(mode="json")
        if isinstance(record, SessionState):
            # Activity extends idle expiry without changing session identity or authority.
            data.pop("expires_at")
            data.pop("idle_expires_at")
        for key, value in data.items():
            if isinstance(value, list):
                data[key] = sorted(value)
        values.append(data)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _permission(kind: ResourceKind, action: Action) -> str:
    if kind in (ResourceKind.ENTITY, ResourceKind.MEMORY):
        return "memory.write" if action == Action.WRITE else "memory.read"
    return {
        ResourceKind.CONVERSATION: "conversation",
        ResourceKind.NOTE: "notes",
        ResourceKind.TASK: "tasks",
        ResourceKind.REMINDER: "reminders",
    }[kind]


class Evaluator:
    def __init__(self, state: StateReader, *, now: Callable[[], datetime]) -> None:
        self._state = state
        self._now = now

    def _member(self, tenant_id: ResourceId, user_id: FirebaseUid) -> Membership:
        member = self._state.membership(tenant_id, user_id)
        if (
            member is None
            or member.tenant_id != tenant_id
            or member.user_id != user_id
            or member.status != "active"
        ):
            raise PolicyDenied()
        return member

    def _device(self, tenant_id: ResourceId, subject: DeviceSubject) -> DeviceState:
        device = self._state.device(tenant_id, subject.device_id)
        if (
            device is None
            or device.tenant_id != tenant_id
            or device.id != subject.device_id
            or device.status != "active"
            or device.connection_generation != subject.connection_generation
        ):
            raise PolicyDenied()
        return device

    def principal(self, tenant_id: ResourceId, subject: Subject) -> Principal:
        """Rebuild from trusted current state; previous TenantContext is not an input."""
        tenant = self._state.tenant(tenant_id)
        if tenant is None or tenant.tenant_id != tenant_id or tenant.status != "active":
            raise PolicyDenied()
        if isinstance(subject, UserSubject):
            member = self._member(tenant_id, subject.user_id)
            stamp = _stamp(tenant, member)
            return Principal(
                TenantContext(
                    tenant_id=tenant_id,
                    actor=UserActorContext(
                        actor_id=subject.user_id,
                        allowed_scopes=USER_SCOPES,
                        authorization_version=member.authorization_version,
                    ),
                ),
                subject,
                stamp,
                subject.user_id,
                member.role,
                member,
            )
        if not isinstance(subject, DeviceSubject):
            raise PolicyDenied()
        device = self._device(tenant_id, subject)
        session = None
        session_member: Membership | None = None
        scopes = device.allowed_scopes & USER_SCOPES
        records: list[BaseModel] = [tenant, device]
        if subject.private_session_id is not None:
            session = self._state.session(tenant_id, subject.private_session_id)
            now = normalize_utc(self._now())
            if (
                session is None
                or session.tenant_id != tenant_id
                or session.id != subject.private_session_id
                or session.device_id != device.id
                or session.connection_generation != device.connection_generation
                or session.status != "active"
                or now >= min(session.expires_at, session.idle_expires_at)
            ):
                raise PolicyDenied()
            session_member = self._member(tenant_id, session.user_id)
            # `shared` belongs to the device's visibility grant, not the session's
            # selectable operation enum. A session restricts operations, while the
            # device's shared permission remains necessary for shared resources.
            scopes &= session.allowed_scopes | {"shared"}
            records.extend((session, session_member))
        stamp = _stamp(*records)
        return Principal(
            TenantContext(
                tenant_id=tenant_id,
                actor=DeviceActorContext(
                    actor_id=device.id,
                    allowed_scopes=scopes,
                    authorization_version=device.authorization_version,
                    private_session=None
                    if session is None
                    else PrivateSessionContext(
                        private_session_id=session.id,
                        user_id=session.user_id,
                        expires_at=session.expires_at,
                        idle_expires_at=session.idle_expires_at,
                    ),
                ),
            ),
            subject,
            stamp,
            None if session_member is None else session_member.user_id,
            None if session_member is None else session_member.role,
            session_member,
        )

    def _filters(self, principal: Principal) -> tuple[SearchFilter, ...]:
        scopes = principal.context.actor.allowed_scopes
        device = isinstance(principal.subject, DeviceSubject)
        device_state = (
            self._device(principal.context.tenant_id, principal.subject)
            if isinstance(principal.subject, DeviceSubject)
            else None
        )
        rooms = None if device_state is None else device_state.allowed_room_ids
        entities = None if device_state is None else device_state.allowed_entity_ids
        shared_display = frozenset({DisplayPolicy.SHARED_DISPLAY}) if device else ALL_DISPLAY
        # A private Pi session adds only its user's private data. Shared Pi data still
        # requires shared_display, as specified by the role matrix.
        result = []
        if "shared" in scopes:
            result.append(SearchFilter(Visibility.SHARED, None, shared_display, rooms, entities))
        if principal.user_id is not None:
            result.append(
                SearchFilter(
                    Visibility.PRIVATE,
                    principal.user_id,
                    frozenset({DisplayPolicy.AUTHENTICATED_SESSION}) if device else ALL_DISPLAY,
                    rooms,
                    entities,
                )
            )
        return tuple(result)

    def search(
        self,
        tenant_id: ResourceId,
        subject: Subject,
        kind: ResourceKind,
        *,
        output: Subject | None = None,
        previous: SearchScope | None = None,
    ) -> SearchScope:
        if not isinstance(kind, ResourceKind):
            raise PolicyDenied()
        principal = self.principal(tenant_id, subject)
        if _permission(kind, Action.READ) not in principal.context.actor.allowed_scopes:
            raise PolicyDenied()
        filters = self._filters(principal)
        output_stamp = None
        if output is not None:
            destination = self._destination(principal, output, kind)
            output_stamp = destination.stamp
            filters = tuple(
                SearchFilter(
                    first.visibility,
                    first.owner_user_id,
                    first.display_policies & second.display_policies,
                    _intersect(first.room_ids, second.room_ids),
                    _intersect(first.entity_ids, second.entity_ids),
                )
                for first in filters
                for second in self._filters(destination)
                if first.visibility == second.visibility
                and first.owner_user_id == second.owner_user_id
                and first.display_policies & second.display_policies
            )
        result = SearchScope(principal, filters, kind, output_stamp)
        if previous is not None and (
            previous.principal.stamp != principal.stamp
            or previous.principal.subject != subject
            or previous.kind != kind
            or previous.filters != filters
            or previous.output_stamp != output_stamp
        ):
            raise PolicyDenied()
        return result

    def _destination(self, principal: Principal, output: Subject, kind: ResourceKind) -> Principal:
        destination = self.principal(principal.context.tenant_id, output)
        if _permission(kind, Action.READ) not in destination.context.actor.allowed_scopes:
            raise PolicyDenied()
        if isinstance(output, UserSubject) or destination.user_id is not None:
            if destination.user_id != principal.user_id:
                raise PolicyDenied()
        if isinstance(output, DeviceSubject):
            device = self._device(principal.context.tenant_id, output)
            if not device.capabilities & {"display", "audio_output"}:
                raise PolicyDenied()
        return destination

    def _readable(self, principal: Principal, resource: ResourceState) -> None:
        if not any(f.matches(resource) for f in self._filters(principal)):
            raise PolicyDenied()
        if isinstance(principal.subject, DeviceSubject):
            device = self._device(principal.context.tenant_id, principal.subject)
            if (
                resource.room_id is not None and resource.room_id not in device.allowed_room_ids
            ) or (
                resource.entity_id is not None
                and resource.entity_id not in device.allowed_entity_ids
            ):
                raise PolicyDenied()

    def resource(
        self,
        tenant_id: ResourceId,
        subject: Subject,
        ref: ResourceRef,
        *,
        action: Action = Action.READ,
        output: Subject | None = None,
        previous: ResourceGrant | None = None,
    ) -> ResourceGrant:
        if not isinstance(action, Action):
            raise PolicyDenied()
        principal = self.principal(tenant_id, subject)
        if ref.tenant_id != tenant_id:
            raise PolicyDenied()
        resource = self._state.resource(ref)
        if resource is None or resource.ref != ref or resource.deleted:
            raise PolicyDenied()
        self._readable(principal, resource)
        if _permission(ref.kind, action) not in principal.context.actor.allowed_scopes:
            raise PolicyDenied()
        if action == Action.MODEL_READ and resource.suppress_reuse:
            raise PolicyDenied()
        if action == Action.POST and ref.kind != ResourceKind.CONVERSATION:
            raise PolicyDenied(hidden=False)
        if action == Action.WRITE:
            if principal.user_id is None:
                raise PolicyDenied(hidden=False)
            if resource.scope.visibility == Visibility.SHARED and (
                resource.created_by != principal.user_id
                and principal.role not in (Role.ADMIN, Role.OWNER)
            ):
                raise PolicyDenied(hidden=False)
        output_stamp = None
        if output is not None:
            destination = self._destination(principal, output, ref.kind)
            self._readable(destination, resource)
            output_stamp = destination.stamp
        result = ResourceGrant(principal, resource, action, output_stamp)
        if previous is not None and (
            previous.principal.stamp != principal.stamp
            or previous.principal.subject != subject
            or previous.resource != resource
            or previous.action != action
            or previous.output_stamp != output_stamp
        ):
            raise PolicyDenied()
        return result

    def worker(self, saved: ResourceGrant, *, output: Subject | None = None) -> ResourceGrant:
        """For an IAM-authenticated worker with a trusted persisted initiator/source receipt.

        Recheck before external calls AND before result writes. Never accept this receipt
        from a task payload without reading its authoritative job document first.
        """
        return self.resource(
            saved.context.tenant_id,
            saved.principal.subject,
            saved.ref,
            action=Action(saved.action),
            output=output,
            previous=saved,
        )

    def management(
        self,
        tenant_id: ResourceId,
        subject: Subject,
        action: ManagementAction,
        *,
        target_user_id: FirebaseUid | None = None,
        new_role: Role | None = None,
    ) -> Principal:
        """Tenant-only prerequisites. Destructive services also require recent auth/audit.

        Owner changes require an owner; IAM-06 additionally reserves the tenant and
        preserves an effective owner before preparing any independent ledger intent.
        This method never grants access to any target member's private content.
        """
        principal = self.principal(tenant_id, subject)
        if not isinstance(action, ManagementAction):
            raise PolicyDenied(hidden=False)
        if new_role is not None and not isinstance(new_role, Role):
            raise PolicyDenied(hidden=False)
        if not isinstance(subject, UserSubject) or principal.role not in (Role.ADMIN, Role.OWNER):
            raise PolicyDenied(hidden=False)
        if action == ManagementAction.DELETE_ACCOUNT:
            raise PolicyDenied(hidden=False)
        if action in (ManagementAction.DELETE_TENANT, ManagementAction.TRANSFER_OWNER):
            if principal.role != Role.OWNER:
                raise PolicyDenied(hidden=False)
        if action in (
            ManagementAction.CHANGE_ROLE,
            ManagementAction.REMOVE_MEMBER,
            ManagementAction.TRANSFER_OWNER,
        ):
            if target_user_id is None:
                raise PolicyDenied(hidden=False)
            target = self._member(tenant_id, target_user_id)
            if target.role == Role.OWNER and principal.role != Role.OWNER:
                raise PolicyDenied(hidden=False)
            if target.user_id == principal.user_id and not (
                action == ManagementAction.CHANGE_ROLE
                and principal.role == Role.OWNER
                and new_role in (Role.ADMIN, Role.MEMBER)
            ):
                raise PolicyDenied(hidden=False)
            if action == ManagementAction.TRANSFER_OWNER and target.role == Role.OWNER:
                raise PolicyDenied(hidden=False)
            if action == ManagementAction.CHANGE_ROLE:
                if new_role is None or (new_role == Role.OWNER and principal.role != Role.OWNER):
                    raise PolicyDenied(hidden=False)
        if action == ManagementAction.INVITE:
            if new_role is None or (new_role == Role.OWNER and principal.role != Role.OWNER):
                raise PolicyDenied(hidden=False)
        return principal

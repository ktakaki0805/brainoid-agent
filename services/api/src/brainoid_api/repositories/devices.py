"""Owner/admin device metadata, serialized authorization changes and revocation."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Self
from uuid import uuid4

from brainoid_contracts import ResourceId
from brainoid_policy.control_ledger import (
    Change,
    ControlLedger,
    DeviceTarget,
    LedgerError,
    OperationScope,
)
from brainoid_policy.models import DeviceState, ManagementAction, UserSubject
from pydantic import Field, JsonValue, model_validator

from brainoid_api.auth.authorization import TransactionPolicyState, policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.common.access import AuthorizedUserScope, fingerprint
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.repositories.device_credentials import Device, Input, _key
from brainoid_api.repositories.tenants import Name, Tenants
from brainoid_api.services.device_authorization import validate_entities


class DevicePatch(Input):
    name: Name | None = None
    allowed_scopes: Annotated[list[OperationScope], Field(max_length=10)] | None = None
    allowed_entity_ids: Annotated[list[ResourceId], Field(max_length=100)] | None = None
    allowed_room_ids: Annotated[list[ResourceId], Field(max_length=100)] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, f) is None for f in self.model_fields_set
        ):
            raise ValueError("Provide at least one non-null device setting")
        return self


class DevicePage(Input):
    items: list[Device]
    next_cursor: str | None


class Devices:
    def __init__(
        self,
        store: TransactionStore,
        tenants: Tenants,
        ledger: ControlLedger,
        cursors: Cursors,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store, self._tenants, self._ledger = store, tenants, ledger
        self._cursors, self._now = cursors, now
        self._changes = AuthzChanges(store, ledger)

    def _authorize(
        self, tx: Transaction, identity: AuthenticatedIdentity, tenant: ResourceId
    ) -> AuthorizedUserScope:
        user = self._tenants.authorize_user(tx, identity)
        policy = transaction_policy(
            tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
        )
        principal = policy_call(
            lambda: policy.management(
                tenant, UserSubject(user_id=identity.user_id), ManagementAction.MANAGE_DEVICES
            )
        )
        return user.model_copy(
            update={"authorization_stamp": fingerprint([user.authorization_stamp, principal.stamp])}
        )

    def _document(self, tx: Transaction, tenant: ResourceId, device: ResourceId) -> Document:
        doc = tx.get(f"tenants/{tenant}/devices/{device}")
        if doc is None or doc.get("tenant_id") != tenant or doc.get("id") != device:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return doc

    def _state(self, tx: Transaction, tenant: ResourceId, device: ResourceId) -> DeviceState:
        state = TransactionPolicyState(tx, ledger=self._ledger, allow_untracked_bootstrap=True)
        result = policy_call(lambda: state.device(tenant, device))
        if result is None or result.tenant_id != tenant or result.id != device:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return result

    def _view(self, tx: Transaction, tenant: ResourceId, device: ResourceId) -> Device:
        doc = self._document(tx, tenant, device)
        state = self._state(tx, tenant, device)
        return Device.model_validate(
            {**{f: doc[f] for f in Device.model_fields}, "status": state.status}
        )

    def get(
        self, identity: AuthenticatedIdentity, tenant: ResourceId, device: ResourceId
    ) -> Device:
        def read(tx: Transaction) -> Device:
            self._authorize(tx, identity, tenant)
            return self._view(tx, tenant, device)

        return self._store.run(read)

    def list(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> DevicePage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> DevicePage:
            user = self._authorize(tx, identity, tenant)
            devices = []
            for path, doc in tx.collection(f"tenants/{tenant}/devices"):
                if path != f"tenants/{tenant}/devices/{doc.get('id')}":
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                devices.append(self._view(tx, tenant, ResourceId(doc["id"])))
            devices.sort(key=lambda d: d.id)
            current = user.model_copy(
                update={
                    "authorization_stamp": fingerprint(
                        [user.authorization_stamp, [d.model_dump(mode="json") for d in devices]]
                    )
                }
            )
            query: JsonValue = {"operation": "devices.list", "tenant": tenant, "limit": limit}
            position = (
                ""
                if cursor is None
                else self._cursors.read(cursor, query=query, authorize=lambda: current)
            )
            remaining = [d for d in devices if d.id > position]
            next_cursor = (
                self._cursors.issue(
                    position=remaining[limit - 1].id, query=query, authorize=lambda: current
                )
                if len(remaining) > limit
                else None
            )
            return DevicePage(items=remaining[:limit], next_cursor=next_cursor)

        return self._store.run(read)

    def update(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        device: ResourceId,
        body: DevicePatch,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> Device:
        return self._mutate(identity, tenant, device, body, key, if_match, request_id)

    def revoke(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        device: ResourceId,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> None:
        self._mutate(identity, tenant, device, None, key, if_match, request_id)

    def _check(
        self, tx: Transaction, identity: AuthenticatedIdentity, path: str, operation: Document
    ) -> tuple[Document, bool]:
        tenant, device = ResourceId(operation["tenant_id"]), ResourceId(operation["device_id"])
        user = self._authorize(tx, identity, tenant)
        saved = tx.get(path)
        if (
            saved is None
            or {k: v for k, v in saved.items() if k != "complete"}
            != {k: v for k, v in operation.items() if k != "complete"}
            or saved["actor_user_id"] != identity.user_id
            or saved["actor_stamp"] != user.authorization_stamp
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        doc = self._document(tx, tenant, device)
        before = operation["before"]
        if not saved["complete"] and doc.get("device_mutation") != operation["id"]:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        change = Change.model_validate(operation["change"]) if operation["change"] else None
        expected = dict(before)
        active = False
        if change is not None:
            entry = self._ledger.latest(change.subject_key)
            if entry is not None and entry.change == change:
                if doc.get("authorization_version") == change.authorization_version:
                    target = change.target.model_dump(mode="json", exclude={"kind", "device_id"})
                    expected.update(target)
                    expected.update(
                        authorization_version=change.authorization_version,
                        authorization_change=change.model_dump(mode="json"),
                        authorization_operation_id=change.operation_id,
                        connection_generation=before["connection_generation"] + 1,
                    )
                    if doc.get("status") not in ("suspended", change.target.status):
                        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                    expected["status"] = doc["status"]
                    active = entry.state == "committed" and doc["status"] == change.target.status
            elif self._state(tx, tenant, device).status != "active":
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        else:
            active = self._state(tx, tenant, device).status == "active"
        if saved["complete"]:
            expected.update(operation["metadata"])
            expected["version"] = before["version"] + 1
            expected["updated_at"] = operation["at"]
        if {k: v for k, v in doc.items() if k != "device_mutation"} != {
            k: v for k, v in expected.items() if k != "device_mutation"
        }:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if change is not None and change.target.status == "active":
            assert isinstance(change.target, DeviceTarget)
            validate_entities(
                tx, tenant, change.target.allowed_entity_ids, change.target.allowed_room_ids
            )
        return doc, active

    def _mutate(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        device: ResourceId,
        body: DevicePatch | None,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> Device:
        key = _key(key)
        expected = require_if_match(if_match)
        patch = {} if body is None else body.model_dump(mode="json", exclude_unset=True)
        for field in ("allowed_scopes", "allowed_entity_ids", "allowed_room_ids"):
            if field in patch:
                patch[field] = sorted(set(patch[field]))
        binding = fingerprint([device, expected, body is None, patch])
        path = f"tenants/{tenant}/device_operations/{fingerprint([identity.user_id, key])}"

        def reserve(tx: Transaction) -> Document:
            user = self._authorize(tx, identity, tenant)
            saved = tx.get(path)
            if saved is not None:
                if saved["binding"] != binding:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                self._check(tx, identity, path, saved)
                return saved
            doc = self._document(tx, tenant, device)
            state = self._state(tx, tenant, device)
            if doc.get("device_mutation") is not None:
                raise APIError(ErrorCode.CONFLICT)
            if doc["version"] != expected:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if state.status != "active":
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            target = DeviceTarget(
                device_id=device,
                status="revoked" if body is None else "active",
                allowed_scopes=tuple(patch.get("allowed_scopes", sorted(state.allowed_scopes))),
                allowed_entity_ids=tuple(
                    patch.get("allowed_entity_ids", sorted(state.allowed_entity_ids))
                ),
                allowed_room_ids=tuple(
                    patch.get("allowed_room_ids", sorted(state.allowed_room_ids))
                ),
            )
            if body is not None:
                validate_entities(tx, tenant, target.allowed_entity_ids, target.allowed_room_ids)
            changed = body is None or any(
                set(getattr(target, f)) != set(getattr(state, f))
                for f in ("allowed_scopes", "allowed_entity_ids", "allowed_room_ids")
            )
            operation_id = ResourceId(str(uuid4()))
            change = None
            if changed:
                reduced = body is None or any(
                    not set(getattr(state, f)) <= set(getattr(target, f))
                    for f in ("allowed_scopes", "allowed_entity_ids", "allowed_room_ids")
                )
                change = Change(
                    operation_id=operation_id,
                    tenant_id=tenant,
                    expected_version=state.authorization_version,
                    authorization_version=state.authorization_version + 1,
                    effect="deny" if reduced else "grant",
                    target=target,
                )
            operation = {
                "id": operation_id,
                "tenant_id": tenant,
                "device_id": device,
                "actor_user_id": identity.user_id,
                "actor_stamp": user.authorization_stamp,
                "binding": binding,
                "before": doc,
                "change": None if change is None else change.model_dump(mode="json"),
                "metadata": {"name": patch["name"]} if "name" in patch else {},
                "at": self._now(),
                "complete": False,
            }
            tx.create(path, operation)
            tx.set(f"tenants/{tenant}/devices/{device}", {**doc, "device_mutation": operation_id})
            tx.create(
                f"tenants/{tenant}/audit_events/{operation_id}",
                {
                    "actor_user_id": identity.user_id,
                    "target_device_id": device,
                    "result": "pending",
                    "request_id": request_id,
                    "at": self._now(),
                },
            )
            return operation

        try:
            operation = self._store.run(reserve)
            if not operation["complete"] and operation["change"] is not None:
                change = Change.model_validate(operation["change"])

                def authorize(tx: Transaction, proposed: Change) -> None:
                    if proposed != change:
                        raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                    self._check(tx, identity, path, operation)

                self._changes.execute(change, authorize=authorize)

            def finish(tx: Transaction) -> Device:
                doc, active = self._check(tx, identity, path, operation)
                if not active:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                saved = tx.get(path)
                assert saved is not None
                audit_path = f"tenants/{tenant}/audit_events/{operation['id']}"
                audit = tx.get(audit_path)
                if audit is None:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                if not saved["complete"]:
                    doc = {
                        **doc,
                        **operation["metadata"],
                        "version": operation["before"]["version"] + 1,
                        "updated_at": operation["at"],
                        "device_mutation": None,
                    }
                    tx.set(f"tenants/{tenant}/devices/{device}", doc)
                    tx.set(path, {**saved, "complete": True})
                    tx.set(audit_path, {**audit, "result": "succeeded", "at": self._now()})
                return Device.model_validate({f: doc[f] for f in Device.model_fields})

            return self._store.run(finish)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

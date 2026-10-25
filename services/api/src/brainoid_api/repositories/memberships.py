"""Tenant-serialized, resumable membership changes with a surviving effective owner."""

import builtins
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from brainoid_contracts import FirebaseUid, ResourceId, Version
from brainoid_policy.control_ledger import Change, ControlLedger, LedgerError, MemberTarget
from brainoid_policy.models import ManagementAction, Membership, Role, UserSubject
from brainoid_policy.scopes import PolicyDenied
from pydantic import BaseModel, ConfigDict, JsonValue

from brainoid_api.auth.authorization import TransactionPolicyState, policy_call, transaction_policy
from brainoid_api.auth.context import AuthenticatedIdentity, require_recent_authentication
from brainoid_api.common.access import fingerprint
from brainoid_api.common.pagination import Cursors, page_limit
from brainoid_api.common.transactions import Document, Transaction, TransactionStore
from brainoid_api.common.versioning import require_if_match
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.authz_changes import AuthzChanges
from brainoid_api.repositories.tenants import Tenants


class MembershipPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    role: Role


class OwnerTransfer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    target_user_id: FirebaseUid
    target_version: Version


class MembershipView(MembershipPatch):
    user_id: FirebaseUid
    status: Literal["active", "suspended", "revoked"]
    version: Version


class MembershipPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: list[MembershipView]
    next_cursor: str | None


class Memberships:
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

    def _state(self, tx: Transaction) -> TransactionPolicyState:
        return TransactionPolicyState(tx, ledger=self._ledger, allow_untracked_bootstrap=True)

    def _member(self, tx: Transaction, tenant: ResourceId, uid: FirebaseUid) -> Membership:
        try:
            member = self._state(tx).membership(tenant, uid)
        except PolicyDenied:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN) from None
        if member is None or member.tenant_id != tenant:
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return member

    def _members(self, tx: Transaction, tenant: ResourceId) -> list[Membership]:
        records = tx.collection(f"tenants/{tenant}/memberships")
        members = [self._member(tx, tenant, FirebaseUid(doc["user_id"])) for _, doc in records]
        if len({m.user_id for m in members}) != len(members):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return sorted(members, key=lambda m: m.user_id)

    @staticmethod
    def _view(member: Membership) -> MembershipView:
        return MembershipView(
            user_id=member.user_id,
            role=member.role,
            status="revoked"
            if member.status == "revoked"
            else "active"
            if member.status == "active"
            else "suspended",
            version=member.authorization_version,
        )

    def _tenant(self, tx: Transaction, tenant: ResourceId) -> Document:
        document = tx.get(f"tenants/{tenant}")
        if document is None or document.get("id") != tenant or document.get("status") != "active":
            raise APIError(ErrorCode.RESOURCE_NOT_FOUND)
        return document

    def list(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> MembershipPage:
        limit = page_limit(limit)

        def read(tx: Transaction) -> MembershipPage:
            user = self._tenants.authorize_user(tx, identity)
            policy = transaction_policy(
                tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
            )
            principal = policy_call(
                lambda: policy.management(
                    tenant, UserSubject(user_id=identity.user_id), ManagementAction.UPDATE_TENANT
                )
            )
            members = self._members(tx, tenant)
            binding = fingerprint(
                [
                    user.authorization_stamp,
                    principal.stamp,
                    [m.model_dump(mode="json") for m in members],
                ]
            )
            current = user.model_copy(update={"authorization_stamp": binding})
            query: JsonValue = {"operation": "memberships.list", "tenant": tenant, "limit": limit}
            position = (
                ""
                if cursor is None
                else self._cursors.read(cursor, query=query, authorize=lambda: current)
            )
            # Hash order keys keep arbitrary UIDs out of signed cursor positions.
            ordered = sorted((fingerprint([m.user_id]), m) for m in members)
            remaining = [(key, m) for key, m in ordered if key > position]
            next_cursor = None
            if len(remaining) > limit:
                next_cursor = self._cursors.issue(
                    position=remaining[limit - 1][0], query=query, authorize=lambda: current
                )
            return MembershipPage(
                items=[self._view(m) for _, m in remaining[:limit]], next_cursor=next_cursor
            )

        return self._store.run(read)

    def update(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        uid: FirebaseUid,
        body: MembershipPatch,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> MembershipView:
        return self._mutate(
            identity, tenant, uid, "role", body.role, None, key, if_match, request_id
        )

    def revoke(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        uid: FirebaseUid,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> None:
        self._mutate(identity, tenant, uid, "revoke", None, None, key, if_match, request_id)

    def transfer(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        body: OwnerTransfer,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> MembershipView:
        return self._mutate(
            identity,
            tenant,
            body.target_user_id,
            "transfer",
            Role.OWNER,
            body.target_version,
            key,
            if_match,
            request_id,
        )

    def _change(
        self, tenant: ResourceId, before: Membership, role: Role, *, revoke: bool = False
    ) -> Change:
        rank = {Role.MEMBER: 0, Role.ADMIN: 1, Role.OWNER: 2}
        return Change(
            operation_id=ResourceId(str(uuid4())),
            tenant_id=tenant,
            expected_version=before.authorization_version,
            authorization_version=before.authorization_version + 1,
            effect="deny" if revoke or rank[role] < rank[before.role] else "grant",
            target=MemberTarget(
                user_id=before.user_id, role=role, status="revoked" if revoke else "active"
            ),
        )

    def _check_change(self, tx: Transaction, change: Change, before: Membership) -> bool:
        """Accept only the original current authority or this exact operation's progress."""
        assert isinstance(change.target, MemberTarget)
        docs = tx.find(f"tenants/{change.tenant_id}/memberships", "user_id", change.target.user_id)
        if len(docs) != 1:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        doc = docs[0]
        latest = self._ledger.latest(change.subject_key)
        if latest is not None and latest.change == change:
            # A pending fence is already effective even before its DB application.
            if doc.get("authorization_version") == before.authorization_version:
                if any(doc.get(k) != v for k, v in before.model_dump(mode="json").items()):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                return False
            if (
                doc.get("authorization_change") != change.model_dump(mode="json")
                or doc.get("authorization_operation_id") != change.operation_id
                or doc.get("authorization_version") != change.authorization_version
                or doc.get("tenant_id") != change.tenant_id
                or doc.get("role") != change.target.role
                or doc.get("status") not in ("suspended", change.target.status)
            ):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            return latest.state == "committed" and doc["status"] == change.target.status
        if self._member(tx, change.tenant_id, change.target.user_id) != before:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return False

    def _authorize(
        self,
        tx: Transaction,
        identity: AuthenticatedIdentity,
        path: str,
        operation: Document,
        *,
        proposed: Change | None = None,
    ) -> builtins.list[bool]:
        user = self._tenants.authorize_user(tx, identity)
        tenant = ResourceId(operation["tenant_id"])
        current = self._tenant(tx, tenant)
        saved = tx.get(path)
        if (
            saved is None
            or {k: v for k, v in saved.items() if k != "complete"}
            != {k: v for k, v in operation.items() if k != "complete"}
            or saved["actor_user_id"] != identity.user_id
            or user.authorization_stamp != saved["user_stamp"]
            or fingerprint([current.get("policy_version"), current.get("deletion_epoch")])
            != saved["tenant_stamp"]
            or (not saved["complete"] and current.get("membership_mutation") != saved["id"])
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if operation["action"] in ("revoke", "transfer"):
            require_recent_authentication(identity, now=self._now())
        changes = [Change.model_validate(c) for c in saved["changes"]]
        originals = [Membership.model_validate(m) for m in saved["before"]]
        progress = [self._check_change(tx, c, m) for c, m in zip(changes, originals, strict=True)]
        actor = Membership.model_validate(saved["actor"])
        own_change = next(
            (
                c
                for c in changes
                if isinstance(c.target, MemberTarget) and c.target.user_id == identity.user_id
            ),
            None,
        )
        if own_change is None and self._member(tx, tenant, identity.user_id) != actor:
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if own_change is not None:
            assert isinstance(own_change.target, MemberTarget)
            if own_change.target.role == Role.OWNER:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if proposed is not None:
            if proposed not in changes or not all(progress[: changes.index(proposed)]):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            # Count effective owners through the same guard used by normal API reads.
            # Exclude the changed subject: even a grant suspends it until activation.
            assert isinstance(proposed.target, MemberTarget)
            if not progress[changes.index(proposed)] and not any(
                m.status == "active"
                and m.role == Role.OWNER
                and m.user_id != proposed.target.user_id
                for m in self._members(tx, tenant)
            ):
                raise APIError(ErrorCode.CONFLICT)
        return progress

    def _mutate(
        self,
        identity: AuthenticatedIdentity,
        tenant: ResourceId,
        uid: FirebaseUid,
        action: Literal["role", "revoke", "transfer"],
        role: Role | None,
        target_version: int | None,
        key: str | None,
        if_match: str | None,
        request_id: str,
    ) -> MembershipView:
        if key is None or not 16 <= len(key) <= 128:
            raise APIError(ErrorCode.INVALID_REQUEST)
        expected = require_if_match(if_match)
        binding = fingerprint([action, uid, role, expected, target_version])
        path = f"tenants/{tenant}/membership_operations/{fingerprint([identity.user_id, key])}"

        def reserve(tx: Transaction) -> Document:
            user = self._tenants.authorize_user(tx, identity)
            current = self._tenant(tx, tenant)
            saved = tx.get(path)
            if saved is not None:
                if saved["binding"] != binding:
                    raise APIError(ErrorCode.IDEMPOTENCY_CONFLICT)
                self._authorize(tx, identity, path, saved)
                return saved
            if current.get("membership_mutation") is not None:
                raise APIError(ErrorCode.CONFLICT)
            policy = transaction_policy(
                tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
            )
            management = {
                "role": ManagementAction.CHANGE_ROLE,
                "revoke": ManagementAction.REMOVE_MEMBER,
                "transfer": ManagementAction.TRANSFER_OWNER,
            }[action]
            policy_call(
                lambda: policy.management(
                    tenant,
                    UserSubject(user_id=identity.user_id),
                    management,
                    target_user_id=uid,
                    new_role=role,
                )
            )
            if action in ("revoke", "transfer"):
                require_recent_authentication(identity, now=self._now())
            actor, target = (
                self._member(tx, tenant, identity.user_id),
                self._member(tx, tenant, uid),
            )
            if (actor if action == "transfer" else target).authorization_version != expected:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if action == "transfer" and target.authorization_version != target_version:
                raise APIError(ErrorCode.VERSION_CONFLICT)
            if target.status != "active" or (action == "role" and target.role == role):
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
            changes = [self._change(tenant, target, role or target.role, revoke=action == "revoke")]
            before = [target]
            if action == "transfer":
                changes.append(self._change(tenant, actor, Role.MEMBER))
                before.append(actor)
            if not any(
                m.status == "active" and m.role == Role.OWNER and m.user_id != uid
                for m in self._members(tx, tenant)
            ):
                raise APIError(ErrorCode.CONFLICT)
            operation = {
                "id": str(uuid4()),
                "tenant_id": tenant,
                "actor_user_id": identity.user_id,
                "user_stamp": user.authorization_stamp,
                "actor": actor.model_dump(mode="json"),
                "tenant_stamp": fingerprint(
                    [current.get("policy_version"), current.get("deletion_epoch")]
                ),
                "binding": binding,
                "action": action,
                "changes": [c.model_dump(mode="json") for c in changes],
                "before": [m.model_dump(mode="json") for m in before],
                "complete": False,
            }
            tx.create(path, operation)
            tx.set(f"tenants/{tenant}", {**current, "membership_mutation": operation["id"]})
            tx.create(
                f"tenants/{tenant}/audit_events/{operation['id']}",
                {
                    "actor_user_id": identity.user_id,
                    "target_user_ids": [m.user_id for m in before],
                    "result": "pending",
                    "request_id": request_id,
                    "at": self._now(),
                },
            )
            return operation

        try:
            operation = self._store.run(reserve)
            if not operation["complete"]:
                for proposed in [Change.model_validate(c) for c in operation["changes"]]:

                    def authorize(tx: Transaction, change: Change) -> None:
                        self._authorize(tx, identity, path, operation, proposed=change)

                    self._changes.execute(proposed, authorize=authorize)

            def finish(tx: Transaction) -> MembershipView:
                progress = self._authorize(tx, identity, path, operation)
                if not all(progress):
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                result = self._view(self._member(tx, tenant, uid))
                current = self._tenant(tx, tenant)
                audit_path = f"tenants/{tenant}/audit_events/{operation['id']}"
                audit = tx.get(audit_path)
                if audit is None:
                    raise APIError(ErrorCode.OPERATION_FORBIDDEN)
                saved = tx.get(path)
                assert saved is not None
                if not saved["complete"]:
                    tx.set(path, {**saved, "complete": True})
                    tx.set(f"tenants/{tenant}", {**current, "membership_mutation": None})
                    tx.set(audit_path, {**audit, "result": "succeeded", "at": self._now()})
                return result

            return self._store.run(finish)
        except LedgerError:
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None

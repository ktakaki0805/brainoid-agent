"""Framework-independent canonical metadata reader over the shared transaction port."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from brainoid_contracts import FirebaseUid, ResourceId, Scope
from brainoid_contracts.deletion import (
    fenced_document,
    lifecycle_account_path,
    lifecycle_tenant_path,
    membership_deletion_pending,
)
from brainoid_contracts.transactions import Document, Transaction
from pydantic import ValidationError

from brainoid_policy.accounts import AccountUnavailable, account_stamp
from brainoid_policy.control_ledger import ControlLedger, LedgerError, subject_key
from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import (
    DeviceState,
    Membership,
    Record,
    ResourceRef,
    ResourceState,
    SessionState,
    TenantState,
)
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable


class TransactionPolicyState:
    """Read metadata from the same transaction as the protected read/write.

    No resource body, graph role, or request-provided context is projected into policy.
    Missing/invalid/ambiguous authority is denied; caller logs must not serialize errors.
    Persistent deletion fences survive physical purge and stale canonical restores.
    """

    def __init__(
        self,
        transaction: Transaction,
        *,
        ledger: ControlLedger | None = None,
        allow_untracked_bootstrap: bool = False,
    ) -> None:
        self._transaction = transaction
        self._ledger = ledger
        self._allow_untracked = allow_untracked_bootstrap

    def _fence[T: Membership | DeviceState](
        self, state: T | None, document: Document | None
    ) -> T | None:
        if state is None or self._ledger is None:
            return state
        kind = "membership" if isinstance(state, Membership) else "device"
        identity = state.user_id if isinstance(state, Membership) else state.id
        try:
            entry = self._ledger.latest(subject_key(state.tenant_id, kind, identity))
        except LedgerError:
            raise PolicyUnavailable() from None
        tracked = document is not None and (
            "authorization_operation_id" in document or "authorization_change" in document
        )
        if entry is None and self._allow_untracked and not tracked:
            return state
        if (
            entry is not None
            and entry.state == "committed"
            and entry.change.authorization_version == state.authorization_version
        ):
            target = entry.change.target.model_dump(mode="json", exclude={"kind"})
            if kind == "device":
                target["id"] = target.pop("device_id")
            actual = state.model_dump(mode="json")
            # Set-like device scope/ID lists have no semantic order.
            if all(
                set(actual[key]) == set(value)
                if isinstance(value, list)
                else actual.get(key) == value
                for key, value in target.items()
            ):
                return state
        return cast(T, state.model_copy(update={"status": "suspended"}))

    def _parse[T: Record](self, model: type[T], document: Document | None) -> T | None:
        if document is None:
            return None
        try:
            return model.model_validate(
                {field: document[field] for field in model.model_fields if field in document}
            )
        except ValidationError:
            raise PolicyDenied() from None

    def tenant(self, tenant_id: ResourceId) -> TenantState | None:
        if self._transaction.get(lifecycle_tenant_path(tenant_id)) is not None:
            raise PolicyDenied()
        return self._parse(TenantState, self._transaction.get(f"tenants/{tenant_id}"))

    def membership(self, tenant_id: ResourceId, user_id: FirebaseUid) -> Membership | None:
        # Firebase UIDs may contain '/'. Never interpolate them into document paths.
        documents = self._transaction.find(f"tenants/{tenant_id}/memberships", "user_id", user_id)
        if len(documents) > 1:
            raise PolicyDenied()
        document = documents[0] if documents else None
        parsed = self._parse(Membership, document)
        if parsed is not None and (
            self._transaction.get(lifecycle_account_path(user_id)) is not None
            or membership_deletion_pending(self._transaction, tenant_id, user_id)
        ):
            return parsed.model_copy(update={"status": "revoked"})
        return self._fence(parsed, document)

    def device(self, tenant_id: ResourceId, device_id: ResourceId) -> DeviceState | None:
        document = self._transaction.get(f"tenants/{tenant_id}/devices/{device_id}")
        return self._fence(self._parse(DeviceState, document), document)

    def session(self, tenant_id: ResourceId, session_id: ResourceId) -> SessionState | None:
        session = self._parse(
            SessionState,
            self._transaction.get(f"tenants/{tenant_id}/private_sessions/{session_id}"),
        )
        if session is None:
            return None
        slot = self._transaction.get(
            f"tenants/{tenant_id}/private_session_slots/{session.device_id}"
        )
        try:
            stamp = account_stamp(self._transaction, session.user_id)
        except AccountUnavailable:
            raise PolicyDenied() from None
        if (
            slot is None
            or slot.get("session_id") != session.id
            or stamp != session.user_authorization_stamp
        ):
            return session.model_copy(update={"status": "suspended"})
        return session

    def resource(self, ref: ResourceRef) -> ResourceState | None:
        document = self._transaction.get(f"tenants/{ref.tenant_id}/{ref.kind}/{ref.id}")
        return resource_state(
            ref,
            fenced_document(self._transaction, ref.tenant_id, ref.kind, ref.id, document)
            if document is not None
            else None,
        )


def resource_state(ref: ResourceRef, document: Document | None) -> ResourceState | None:
    """Canonical metadata projection, also used to bind an atomic source replacement."""
    if document is None:
        return None
    if document.get("tenant_id") != ref.tenant_id or document.get("id") != ref.id:
        raise PolicyDenied()
    metadata: dict[str, Any] = {**document, "ref": ref}
    if ref.kind == "calendar_proposals":
        # CAL03 intents already bind their canonical owner; no actor is inferred.
        metadata["created_by"] = document.get("owner_user_id")
    try:
        metadata["scope"] = Scope.model_validate(
            {
                "visibility": document.get("visibility"),
                "display_policy": document.get("display_policy"),
            }
        )
    except ValidationError:
        raise PolicyDenied() from None
    try:
        return ResourceState.model_validate(
            {field: metadata[field] for field in ResourceState.model_fields if field in metadata}
        )
    except ValidationError:
        raise PolicyDenied() from None


def transaction_policy(
    transaction: Transaction,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ledger: ControlLedger | None = None,
    allow_untracked_bootstrap: bool = False,
) -> Evaluator:
    return Evaluator(
        TransactionPolicyState(
            transaction, ledger=ledger, allow_untracked_bootstrap=allow_untracked_bootstrap
        ),
        now=now,
    )

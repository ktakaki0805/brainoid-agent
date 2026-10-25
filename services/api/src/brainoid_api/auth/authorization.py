"""Transaction-bound policy adapter; no implicit SDK/client or production connection."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from brainoid_contracts import FirebaseUid, ResourceId, Scope, normalize_utc
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
    UserSubject,
)
from brainoid_policy.scopes import PolicyDenied, ResourceGrant
from pydantic import ValidationError

from brainoid_api.auth.context import SelectedTenantIdentity
from brainoid_api.common.access import AuthorizedScope, fingerprint
from brainoid_api.common.transactions import Document, Transaction
from brainoid_api.errors import APIError, ErrorCode


def policy_call[T](work: Callable[[], T]) -> T:
    """Translate only content-free policy errors into the common public error contract."""
    try:
        return work()
    except PolicyDenied as error:
        raise APIError(ErrorCode(error.code)) from None


def authorized_scope(grant: ResourceGrant) -> AuthorizedScope:
    """Call inside the current authorization callback, after Evaluator.resource().

    Conversion does not reauthorize a saved receipt. Replays and cursor reads must
    create a new evaluator over their current transaction and call policy again.
    """
    return AuthorizedScope(
        context=grant.context,
        scope=grant.scope,
        owner_user_id=grant.owner_user_id,
        authorization_stamp=(
            grant.principal.stamp
            if grant.output_stamp is None
            else fingerprint([grant.principal.stamp, grant.output_stamp])
        ),
    )


def user_subject(selection: SelectedTenantIdentity, *, now: datetime) -> UserSubject:
    """Only consume IAM-01's verified identity. Selection still requires membership."""
    current = normalize_utc(now)
    if current >= selection.identity.expires_at or current < selection.identity.issued_at:
        raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
    return UserSubject(user_id=selection.identity.user_id)


class TransactionPolicyState:
    """Read metadata from the same transaction as the protected read/write.

    No resource body, graph role, or request-provided context is projected into policy.
    Missing/invalid/ambiguous authority is denied; caller logs must not serialize errors.
    Canonical resource tombstones must set deleted before purge (later MEM/IAM flows).
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

    def _fence[T: Membership | DeviceState](self, state: T | None) -> T | None:
        if state is None or self._ledger is None:
            return state
        kind = "membership" if isinstance(state, Membership) else "device"
        identity = state.user_id if isinstance(state, Membership) else state.id
        try:
            entry = self._ledger.latest(subject_key(state.tenant_id, kind, identity))
        except LedgerError:
            raise PolicyDenied() from None
        if entry is None and self._allow_untracked:
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
        return self._parse(TenantState, self._transaction.get(f"tenants/{tenant_id}"))

    def membership(self, tenant_id: ResourceId, user_id: FirebaseUid) -> Membership | None:
        # Firebase UIDs may contain '/'. Never interpolate them into document paths.
        documents = self._transaction.find(f"tenants/{tenant_id}/memberships", "user_id", user_id)
        if len(documents) > 1:
            raise PolicyDenied()
        return self._fence(self._parse(Membership, documents[0] if documents else None))

    def device(self, tenant_id: ResourceId, device_id: ResourceId) -> DeviceState | None:
        return self._fence(
            self._parse(
                DeviceState, self._transaction.get(f"tenants/{tenant_id}/devices/{device_id}")
            )
        )

    def session(self, tenant_id: ResourceId, session_id: ResourceId) -> SessionState | None:
        return self._parse(
            SessionState,
            self._transaction.get(f"tenants/{tenant_id}/private_sessions/{session_id}"),
        )

    def resource(self, ref: ResourceRef) -> ResourceState | None:
        document = self._transaction.get(f"tenants/{ref.tenant_id}/{ref.kind}/{ref.id}")
        if document is None:
            return None
        if document.get("tenant_id") != ref.tenant_id or document.get("id") != ref.id:
            raise PolicyDenied()
        metadata: dict[str, Any] = {**document, "ref": ref}
        try:
            metadata["scope"] = Scope.model_validate(
                {
                    "visibility": document.get("visibility"),
                    "display_policy": document.get("display_policy"),
                }
            )
        except ValidationError:
            raise PolicyDenied() from None
        return self._parse(ResourceState, metadata)


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

"""Current device checks for commands, private sessions and credential operations."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from brainoid_contracts import ResourceId, Version
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.models import DeviceState, DeviceSubject, Record

from brainoid_api.auth.authorization import TransactionPolicyState, policy_call, transaction_policy
from brainoid_api.common.transactions import Transaction
from brainoid_api.errors import APIError, ErrorCode


def validate_entities(
    tx: Transaction,
    tenant: ResourceId,
    entities: Iterable[ResourceId],
    rooms: Iterable[ResourceId] = (),
) -> None:
    """Canonical metadata only; no cross-tenant or deleted identifiers in allowlists."""
    for identifiers, room in ((entities, False), (rooms, True)):
        for identity in identifiers:
            doc = tx.get(f"tenants/{tenant}/entities/{identity}")
            if (
                doc is None
                or doc.get("id") != identity
                or doc.get("tenant_id") != tenant
                or doc.get("deleted") is not False
                or doc.get("status") in ("deleted", "deleting", "revoked")
                or (room and doc.get("kind") != "room")
            ):
                raise APIError(ErrorCode.RESOURCE_NOT_FOUND)


class DeviceBinding(Record):
    """Persist with trusted commands; never accept a binding from model/client input."""

    tenant_id: ResourceId
    device_id: ResourceId
    authorization_version: Version
    connection_generation: Version


class DeviceAuthorization:
    def __init__(
        self, ledger: ControlLedger, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._ledger, self._now = ledger, now

    def current(
        self,
        tx: Transaction,
        binding: DeviceBinding,
        *,
        scope: str | None = None,
        entity_id: ResourceId | None = None,
        private_session_id: ResourceId | None = None,
    ) -> DeviceState:
        """Call in the effect/write transaction, and again immediately before output.

        IAM-09 session creation and APP-06 command dispatch must use this check.
        Existing policy.worker/resource(previous=...) also rechecks saved output grants.
        A new credential or replacement device cannot upgrade an old command binding.
        """
        state = TransactionPolicyState(tx, ledger=self._ledger, allow_untracked_bootstrap=True)
        device = policy_call(lambda: state.device(binding.tenant_id, binding.device_id))
        policy = transaction_policy(
            tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
        )
        policy_call(
            lambda: policy.principal(
                binding.tenant_id,
                DeviceSubject(
                    device_id=binding.device_id,
                    connection_generation=binding.connection_generation,
                    private_session_id=private_session_id,
                ),
            )
        )
        if (
            device is None
            or device.authorization_version != binding.authorization_version
            or (scope is not None and scope not in device.allowed_scopes)
            or (entity_id is not None and entity_id not in device.allowed_entity_ids)
        ):
            raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        if entity_id is not None:
            validate_entities(tx, binding.tenant_id, (entity_id,))
        if private_session_id is not None and scope is not None:
            session = state.session(binding.tenant_id, private_session_id)
            if session is None or scope not in session.allowed_scopes:
                raise APIError(ErrorCode.OPERATION_FORBIDDEN)
        return device

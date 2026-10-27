"""Current device checks for commands, private sessions and credential operations."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from brainoid_contracts import ResourceId
from brainoid_policy.control_ledger import ControlLedger
from brainoid_policy.devices import DeviceBinding as DeviceBinding
from brainoid_policy.devices import current_device
from brainoid_policy.models import DeviceState

from brainoid_api.auth.authorization import policy_call, transaction_policy
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
        policy = transaction_policy(
            tx, ledger=self._ledger, allow_untracked_bootstrap=True, now=self._now
        )
        device = policy_call(
            lambda: current_device(
                policy,
                binding,
                scope=scope,
                entity_id=entity_id,
                private_session_id=private_session_id,
            )
        )
        if entity_id is not None:
            validate_entities(tx, binding.tenant_id, (entity_id,))
        return device

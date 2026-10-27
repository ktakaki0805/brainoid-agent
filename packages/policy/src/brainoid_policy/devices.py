"""Fixed device bindings shared by API and durable business services."""

from collections.abc import Callable
from datetime import datetime

from brainoid_contracts import ResourceId, UtcTimestamp, Version, normalize_utc
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Document, Transaction
from pydantic import TypeAdapter

from brainoid_policy.evaluator import Evaluator
from brainoid_policy.models import DeviceState, DeviceSubject, Record
from brainoid_policy.scopes import PolicyDenied


class DeviceBinding(Record):
    """Server-created immutable binding; never accepted from model/client input."""

    tenant_id: ResourceId
    device_id: ResourceId
    authorization_version: Version
    connection_generation: Version


class CredentialBinding(DeviceBinding):
    """Immutable admission identity without any bearer or rotating refresh state."""

    family_id: ResourceId
    credential_epoch: ResourceId


def current_family(
    tx: Transaction,
    binding: CredentialBinding,
    *,
    credential_epoch: ResourceId | None,
    now: Callable[[], datetime],
) -> Document:
    """Recheck the original family against the independently configured epoch.

    Device/ledger/session authorization is additionally checked by the caller's
    current policy. Refresh rotation keeps this binding; replacement never does.
    """
    family = tx.get("device_credential_families/" + binding.family_id)
    if (
        family is None
        or credential_epoch is None
        or binding.credential_epoch != credential_epoch
        or family.get("epoch") != credential_epoch
        or family.get("status") != "active"
        or any(
            family.get(field) != getattr(binding, field)
            for field in (
                "tenant_id",
                "device_id",
                "authorization_version",
                "connection_generation",
            )
        )
    ):
        raise PolicyDenied()
    expiry = TypeAdapter(UtcTimestamp).validate_python(family.get("expires_at"))

    def live() -> None:
        if normalize_utc(now()) >= expiry:
            raise PolicyDenied()

    live()
    protect(tx, live)
    return {**family, "expires_at": expiry}


def current_device(
    policy: Evaluator,
    binding: DeviceBinding,
    *,
    scope: str | None = None,
    entity_id: ResourceId | None = None,
    private_session_id: ResourceId | None = None,
) -> DeviceState:
    device = policy._state.device(binding.tenant_id, binding.device_id)
    policy.principal(
        binding.tenant_id,
        DeviceSubject(
            device_id=binding.device_id,
            connection_generation=binding.connection_generation,
            private_session_id=private_session_id,
        ),
    )
    if (
        device is None
        or device.authorization_version != binding.authorization_version
        or (scope is not None and scope not in device.allowed_scopes)
        or (entity_id is not None and entity_id not in device.allowed_entity_ids)
    ):
        raise PolicyDenied(hidden=False)
    if private_session_id is not None and scope is not None:
        session = policy._state.session(binding.tenant_id, private_session_id)
        if session is None or scope not in session.allowed_scopes:
            raise PolicyDenied(hidden=False)
    return device

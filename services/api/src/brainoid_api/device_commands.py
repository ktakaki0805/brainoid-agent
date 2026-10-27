"""Durable command delivery, atomic start and result receipts. No provider I/O."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from brainoid_contracts import DeviceActorContext, ResourceId, Visibility, normalize_utc
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Transaction
from brainoid_memory.commands import (
    PRE_START,
    CommandDelivery,
    CommandExecution,
    CommandRecord,
    Commands,
    ObservedState,
    command_path,
)
from brainoid_memory.repository import FactTransaction
from brainoid_policy.models import DeviceSubject
from brainoid_policy.scopes import PolicyDenied
from pydantic import TypeAdapter, ValidationError

from brainoid_api.device_leases import ConnectionBinding
from brainoid_api.device_protocol import ProtocolError
from brainoid_api.repositories.memories import Memories


class DeviceCommands:
    def __init__(self, access: Memories) -> None:
        self.access, self.now = access, access.now

    def _open(
        self, tx: Transaction, binding: ConnectionBinding, identity: ResourceId
    ) -> tuple[CommandRecord, FactTransaction]:
        document = tx.get(command_path(binding.tenant_id, identity))
        try:
            record = CommandRecord.model_validate(document)
        except ValidationError:
            raise PolicyDenied() from None
        if record.device_id != binding.device_id:
            raise PolicyDenied()
        session = None
        if record.delivery is not None:
            delivery = record.delivery
            if (delivery.family_id, delivery.credential_epoch) != (
                binding.identity.family_id,
                binding.identity.credential_epoch,
            ):
                raise PolicyDenied()
            session = delivery.private_session_id
        elif record.visibility == Visibility.PRIVATE:
            if isinstance(record.actor, DeviceSubject):
                session = record.actor.private_session_id
            else:
                slot = tx.get(
                    f"tenants/{binding.tenant_id}/private_session_slots/{binding.device_id}"
                )
                if slot is not None:
                    session = TypeAdapter(ResourceId).validate_python(slot["session_id"])
        if record.visibility == Visibility.PRIVATE and session is None:
            raise PolicyDenied()
        access = self.access._open(tx, binding.identity, binding.tenant_id, session)
        record = Commands.get(access, identity)
        # Original private initiator deadlines must also survive delayed commit/handoff.
        original = access._authority.policy.principal(binding.tenant_id, record.actor)
        actor = original.context.actor
        if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
            expiry = min(actor.private_session.expires_at, actor.private_session.idle_expires_at)
            protect(tx, lambda: self._before(expiry))
        return record, access

    def _before(self, expiry: datetime) -> None:
        if self.now() >= expiry:
            raise PolicyDenied()

    def _save(self, tx: Transaction, record: CommandRecord, **updates: Any) -> CommandRecord:
        updated = CommandRecord.model_validate(
            {
                **record.model_dump(),
                **updates,
                "version": record.version + 1,
                "updated_at": self.now(),
            }
        )
        tx.set(command_path(record.tenant_id, record.id), updated.model_dump(mode="json"))
        return updated

    def deliver(
        self, tx: Transaction, binding: ConnectionBinding, identity: ResourceId
    ) -> dict[str, Any] | None:
        try:
            record, access = self._open(tx, binding, identity)
        except PolicyDenied:
            # A watch is only a hint, never permission to disclose a private record.
            return None
        if record.status not in PRE_START | {"running", "outcome_unknown"}:
            return None
        if self.now() >= record.expires_at:
            if record.status in PRE_START:
                self._save(tx, record, status="expired")
            return None
        protect(tx, lambda: self._before(record.expires_at))
        delivery = record.delivery
        if delivery is None:
            counter_path = (
                f"tenants/{binding.tenant_id}/device_delivery_counters/{binding.device_id}"
            )
            counter = tx.get(counter_path)
            sequence = 1 if counter is None else counter["sequence"] + 1
            subject = access.caller.subject
            assert isinstance(subject, DeviceSubject)
            delivery = CommandDelivery(
                sequence=sequence,
                family_id=binding.identity.family_id,
                credential_epoch=binding.identity.credential_epoch,
                private_session_id=subject.private_session_id,
            )
            access.finish()
            tx.set(counter_path, {"sequence": sequence})
            record = self._save(tx, record, delivery=delivery, status="delivered")
        elif record.status == "queued":
            record = self._save(tx, record, status="delivered")
        return {
            "delivery_seq": delivery.sequence,
            "tenant_id": record.tenant_id,
            "device_id": record.device_id,
            "command_id": record.id,
            "target_entity_id": record.target_entity_id,
            "operation": record.operation.model_dump(mode="json"),
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            **(
                {}
                if delivery.private_session_id is None
                else {"private_session_id": delivery.private_session_id}
            ),
        }

    def start(
        self, tx: Transaction, binding: ConnectionBinding, payload: dict[str, Any]
    ) -> dict[str, Any]:
        identity, execution = ResourceId(payload["command_id"]), ResourceId(payload["execution_id"])
        record, access = self._open(tx, binding, identity)
        lease = tx.get(f"tenants/{binding.tenant_id}/device_connections/{binding.device_id}")
        if lease is None:
            raise PolicyDenied()
        deadline = min(
            record.expires_at, access.caller.valid_to, normalize_utc(lease["expires_at"])
        )
        for subject in (access.caller.subject, record.actor):
            actor = access._authority.policy.principal(binding.tenant_id, subject).context.actor
            if isinstance(actor, DeviceActorContext) and actor.private_session is not None:
                deadline = min(
                    deadline,
                    actor.private_session.expires_at,
                    actor.private_session.idle_expires_at,
                )
        if record.execution is not None:
            deadline = min(deadline, record.execution.expires_at)
        response = {
            "command_id": identity,
            "execution_id": execution,
            "expires_at": (
                record.expires_at if record.execution is None else record.execution.expires_at
            ).isoformat(),
        }
        reason = None
        if self.now() >= deadline:
            reason = "expired"
        elif record.execution is not None and deadline != record.execution.expires_at:
            reason = "authority_deadline_changed"
        elif record.execution_id is not None:
            if record.execution_id != execution or record.execution is None:
                reason = "execution_conflict"
            elif record.status != "running":
                reason = "already_finished"
        elif (
            record.status != "accepted"
            or record.delivery is None
            or record.accepted_execution_id != execution
        ):
            reason = "not_accepted"
        if reason is not None:
            if reason == "expired" and record.status in PRE_START:
                self._save(tx, record, status="expired")
            return {**response, "result": "rejected", "reason_code": reason}
        protect(tx, lambda: self._before(deadline))
        access.finish()
        if record.execution_id is None:
            self._save(
                tx,
                record,
                status="running",
                execution_id=execution,
                execution=CommandExecution(
                    generation=binding.generation, claimed_at=self.now(), expires_at=deadline
                ),
            )
        return {**response, "expires_at": deadline.isoformat(), "result": "granted"}

    @staticmethod
    def _observed(record: CommandRecord, payload: dict[str, Any]) -> bool:
        observed = ObservedState.model_validate(payload.get("observed_state"))
        operation = record.operation
        if payload.get("error_code") is not None:
            return False
        if operation.action == "light.turn_on":
            return (
                observed.power == "on"
                and observed.brightness_pct != 0
                and (
                    operation.parameters.brightness_pct is None
                    or observed.brightness_pct == operation.parameters.brightness_pct
                )
            )
        if operation.action == "light.turn_off":
            return observed.power == "off" and observed.brightness_pct in (None, 0)
        if operation.action == "climate.set_temperature":
            return observed.temperature_c == operation.parameters.temperature_c
        return observed.hvac_mode == operation.parameters.hvac_mode

    def status(
        self, tx: Transaction, binding: ConnectionBinding, payload: dict[str, Any]
    ) -> dict[str, Any]:
        identity, execution = ResourceId(payload["command_id"]), ResourceId(payload["execution_id"])
        record, access = self._open(tx, binding, identity)
        status = payload["status"]
        response = {"command_id": identity, "execution_id": execution}
        rejected = {**response, "result": "rejected", "reason_code": "status_conflict"}
        prior = record.status_receipts.get(status)
        if prior is not None:
            return {**response, "result": "recorded"} if prior == payload else rejected
        if (
            record.delivery is None
            or record.accepted_execution_id not in (None, execution)
            or not record.created_at <= normalize_utc(payload["observed_at"]) <= self.now()
        ):
            return rejected
        updates: dict[str, Any] = {}
        if status == "accepted":
            if record.status not in {"delivered", "accepted"} or record.execution_id is not None:
                return rejected
            if self.now() >= record.expires_at:
                return {**rejected, "reason_code": "expired"}
            protect(tx, lambda: self._before(record.expires_at))
            updates["accepted_execution_id"] = execution
        elif status == "expired":
            if record.status not in PRE_START | {"expired"} or record.execution_id is not None:
                return rejected
            if self.now() < record.expires_at:
                return rejected
            updates["accepted_execution_id"] = execution
        elif status in {"succeeded", "failed", "outcome_unknown"}:
            if (
                record.execution_id != execution
                or record.execution is None
                or record.status not in {"running", "outcome_unknown"}
                or normalize_utc(payload["observed_at"]) < record.execution.claimed_at
            ):
                return rejected
            if status == "succeeded" and not self._observed(record, payload):
                return {**rejected, "reason_code": "observation_required"}
            updates.update(
                observed_state=payload.get("observed_state"), error_code=payload.get("error_code")
            )
        else:
            raise ProtocolError()
        access.finish()
        self._save(
            tx,
            record,
            status=status,
            status_receipts={**record.status_receipts, status: payload},
            **updates,
        )
        return {**response, "result": "recorded"}

    def handler(
        self, kind: str
    ) -> Callable[[Transaction, ConnectionBinding, dict[str, Any]], dict[str, Any]]:
        return self.start if kind == "command_start" else self.status

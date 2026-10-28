"""Committed-state polling and current-authorization delivery for device recovery hints."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from brainoid_contracts import ResourceId
from brainoid_contracts.device_snapshot import DeviceSnapshot
from brainoid_contracts.guards import protect
from brainoid_contracts.transactions import Transaction
from brainoid_memory.notifications import (
    DeliveryState,
    NotificationView,
    acknowledge,
    delivery_path,
    public_notification,
    read_delivery,
    read_notification,
)
from brainoid_policy.scopes import PolicyDenied

from brainoid_api.device_gateway import Delivery, DeviceConnection
from brainoid_api.device_leases import ConnectionBinding
from brainoid_api.device_snapshot import DeviceSnapshots
from brainoid_api.errors import APIError, ErrorCode


class DeviceNotifications:
    def __init__(self, snapshots: DeviceSnapshots):
        self.snapshots = snapshots

    def notification(
        self, tx: Transaction, binding: ConnectionBinding, identity: ResourceId
    ) -> dict[str, Any] | None:
        tenant = binding.tenant_id
        try:
            record = read_notification(tx, tenant, identity)
            session = self.snapshots.sessions.snapshot(tx, binding.identity, persist=False)
            selected = session.id if session and record.visibility == "private" else None
            access = self.snapshots.memories._open(tx, binding.identity, tenant, selected)
            view = NotificationView.model_validate(public_notification(access, identity))
            if view.status in {"displayed", "acknowledged", "expired"}:
                return None
            protect(tx, access.finish)
            receipt = read_delivery(tx, record)
            counter_path = f"tenants/{tenant}/device_delivery_counters/{binding.device_id}"
            counter = tx.get(counter_path)
            sequence = receipt.device_sequence
            if sequence is None:
                sequence = counter["sequence"] + 1 if counter else 1
                access.finish()
                tx.set(counter_path, {"sequence": sequence})
                tx.set(
                    delivery_path(record),
                    receipt.model_copy(update={"device_sequence": sequence}).model_dump(
                        mode="json"
                    ),
                )
            return {
                "notification_id": identity,
                "title": view.title,
                "expires_at": view.expires_at.isoformat(),
                "delivery_seq": sequence,
                **({"private_session_id": selected} if selected else {}),
            }
        except PolicyDenied:
            return None

    def ack(self, tx: Transaction, binding: ConnectionBinding, payload: dict[str, Any]) -> None:
        if payload["status"] != "displayed":
            return None
        identity = ResourceId(payload["notification_id"])
        record = read_notification(tx, binding.tenant_id, identity)
        receipt: DeliveryState = read_delivery(tx, record)
        # A receipt only exists after an authorized delivery was committed.
        if (
            receipt.device_sequence is None
            or record.target.kind != "device"
            or record.target.id != binding.device_id
        ):
            raise PolicyDenied()
        session = self.snapshots.sessions.snapshot(tx, binding.identity, persist=False)
        selected = session.id if session and record.visibility == "private" else None
        access = self.snapshots.memories._open(tx, binding.identity, binding.tenant_id, selected)
        acknowledge(access, identity, "displayed")
        return None

    def hint(self, kind: str, identity: ResourceId) -> Delivery:
        def load(tx: Transaction, binding: ConnectionBinding) -> dict[str, Any] | None:
            if kind == "notification":
                return self.notification(tx, binding, identity)
            session = self.snapshots.sessions.snapshot(tx, binding.identity, persist=False)
            if kind == "session_revoked":
                return (
                    {"private_session_id": identity, "reason": "expired"}
                    if session is None or session.id != identity
                    else None
                )
            if kind == "session_started":
                if session is None or session.id != identity:
                    return None
                return {
                    "private_session_id": session.id,
                    "user_id": session.user_id,
                    "allowed_scopes": session.allowed_scopes,
                    "expires_at": session.expires_at.isoformat(),
                    "idle_expires_at": session.idle_expires_at.isoformat(),
                }
            run = self.snapshots.run(tx, binding.identity, session, identity)
            return run.model_dump(mode="json", exclude_none=True) if run else None

        return Delivery(kind, load)

    @asynccontextmanager
    async def __call__(self, connection: DeviceConnection) -> AsyncIterator[None]:
        async def poll() -> None:
            previous_session = None
            seen: set[tuple[str, ResourceId]] = set()
            cursor: str | None = None

            def read(tx: Transaction, binding: ConnectionBinding) -> DeviceSnapshot:
                return self.snapshots.collect(tx, binding.identity, cursor)

            while True:
                # The transaction result is visible only AFTER commit. No request
                # callback or SDK watch can publish an uncommitted session or run.
                try:
                    page = await connection.run(read)
                except APIError as error:
                    if error.code != ErrorCode.INVALID_REQUEST or cursor is None:
                        raise
                    cursor = None
                    continue
                cursor = page.next_cursor
                session = page.active_private_session
                current_session = session.id if session else None
                if previous_session and previous_session != current_session:
                    await connection.enqueue_wait(self.hint("session_revoked", previous_session))
                candidates = (
                    ({("session_started", current_session)} if current_session else set())
                    | {("run_available", r.run_id) for r in page.assigned_runs}
                    | {("notification", i) for i in page.pending_notification_ids}
                )
                for kind, identity in sorted(candidates - seen):
                    await connection.enqueue_wait(self.hint(kind, identity))
                seen, previous_session = candidates, current_session
                await asyncio.sleep(1)

        task = asyncio.create_task(poll())

        def completed(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                connection.fail(1011)

        task.add_done_callback(completed)
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

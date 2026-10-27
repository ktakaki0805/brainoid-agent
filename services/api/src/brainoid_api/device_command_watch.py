"""Firestore watch scoped by authenticated tenant/device; callbacks carry IDs only."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from threading import Lock
from typing import Any

import anyio
from brainoid_contracts import ResourceId, normalize_utc
from brainoid_memory.commands import PRE_START
from google.cloud.firestore_v1 import Client
from google.cloud.firestore_v1.base_query import FieldFilter
from starlette.concurrency import run_in_threadpool

from brainoid_api.device_commands import DeviceCommands
from brainoid_api.device_gateway import Delivery, DeviceConnection, DeviceGateway
from brainoid_api.device_leases import DeviceLeases
from brainoid_api.device_protocol import ProtocolError

MAX_PENDING_COMMANDS = 256


class CommandWatch:
    def __init__(self, client: Client, commands: DeviceCommands) -> None:
        self.client, self.commands = client, commands

    @asynccontextmanager
    async def __call__(self, connection: DeviceConnection) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        lock = Lock()
        eligible: list[ResourceId] = []
        seen: set[ResourceId] = set()
        changed = asyncio.Event()
        stopped = False
        failed = False
        scheduled = False
        binding = connection.binding

        def snapshot(documents: Any, changes: Any, read_time: Any) -> None:
            nonlocal failed, scheduled, eligible
            # SDK callbacks serialize snapshots. Initial delivery always replays assigned
            # sequences before allocating new ones, independent of document ID order.
            with lock:
                if stopped:
                    return
                candidates = []
                for document in documents:
                    body = document.to_dict()
                    if (
                        body is not None
                        and body.get("status") in PRE_START | {"running", "outcome_unknown"}
                        and normalize_utc(body["expires_at"]) > self.commands.now()
                    ):
                        sequence = (body.get("delivery") or {}).get("sequence")
                        candidates.append(
                            (
                                (
                                    sequence is None,
                                    sequence or 0,
                                    body.get("created_at", ""),
                                    document.id,
                                ),
                                document.id,
                            )
                        )
                        if len(candidates) > MAX_PENDING_COMMANDS:
                            failed = True
                            break
                eligible = [ResourceId(identity) for _, identity in sorted(candidates)]
                seen.intersection_update(eligible)
                if not scheduled:
                    scheduled = True
                    loop.call_soon_threadsafe(changed.set)

        def subscribe() -> Any:
            return (
                self.client.collection(f"tenants/{binding.tenant_id}/commands")
                .where(filter=FieldFilter("device_id", "==", binding.device_id))
                .on_snapshot(snapshot)
            )

        # Keep subscription creation owned even when the request is cancelled.
        async def acquire() -> Any:
            with anyio.CancelScope(shield=True):
                return await run_in_threadpool(subscribe)

        acquisition = asyncio.create_task(acquire())
        watch = None
        pump = None
        try:
            watch = await asyncio.shield(acquisition)

            async def deliver() -> None:
                nonlocal scheduled
                while True:
                    try:
                        await asyncio.wait_for(changed.wait(), 1.0)
                    except TimeoutError:
                        if watch is None or not watch.is_active:
                            raise ProtocolError(1011) from None
                    with lock:
                        batch = [identity for identity in eligible if identity not in seen]
                        seen.update(batch)
                        changed.clear()
                        scheduled = False
                        bad = failed
                    if bad:
                        raise ProtocolError(1013)
                    for identity in batch:

                        def completed(sent: bool, identity: ResourceId = identity) -> None:
                            if not sent:
                                with lock:
                                    seen.discard(identity)

                        await connection.enqueue_wait(
                            Delivery(
                                "command",
                                partial(self.commands.deliver, identity=identity),
                                completed,
                            )
                        )

            pump = asyncio.create_task(deliver())

            def completed(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and (error := task.exception()) is not None:
                    connection.fail(error.code if isinstance(error, ProtocolError) else 1011)

            pump.add_done_callback(completed)
            yield
        finally:
            with lock:
                stopped = True
            with anyio.CancelScope(shield=True):
                if pump is not None:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)
                if watch is None:
                    result = await asyncio.gather(acquisition, return_exceptions=True)
                    if not isinstance(result[0], BaseException):
                        watch = result[0]
                if watch is not None:
                    await run_in_threadpool(watch.unsubscribe)


def command_gateway(
    leases: DeviceLeases, client: Client, commands: DeviceCommands
) -> DeviceGateway:
    def handler(kind: str, result: str) -> Callable[[dict[str, Any]], Delivery]:
        def dispatch(payload: dict[str, Any]) -> Delivery:
            return Delivery(
                result, lambda tx, binding: commands.handler(kind)(tx, binding, payload)
            )

        return dispatch

    return DeviceGateway(
        leases,
        hooks=(CommandWatch(client, commands),),
        handlers={
            "command_start": handler("command_start", "command_start_result"),
            "command_status": handler("command_status", "command_status_ack"),
        },
    )

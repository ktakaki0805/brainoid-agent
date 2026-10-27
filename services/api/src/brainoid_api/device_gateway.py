"""Authenticated WSS lifecycle and server-owned, device-scoped delivery hooks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Any
from uuid import uuid4

import anyio
from brainoid_contracts.guards import release_guard
from brainoid_contracts.transactions import Transaction
from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from brainoid_api.auth.device_tokens import DeviceIdentity
from brainoid_api.device_leases import HEARTBEAT_SECONDS, ConnectionBinding, DeviceLeases
from brainoid_api.device_protocol import SUBPROTOCOL, ProtocolError, decode, encode, envelope
from brainoid_api.errors import APIError, logger

router = APIRouter()
IO_TIMEOUT = 5.0
HELLO_TIMEOUT = 10.0
SEND_TIMEOUT = 5.0
QUEUE_SIZE = 16


@dataclass(frozen=True)
class _Admission:
    """Thread-safe, no-I/O evidence retained through queuing and transaction retries."""

    alive: Callable[[], bool]
    deadline: float
    cancelled: Event = field(default_factory=Event)

    def check(self) -> None:
        if self.cancelled.is_set() or not self.alive() or monotonic() >= self.deadline:
            raise TimeoutError("device_operation_expired")

    def invoke[T](self, call: Callable[[Callable[[], None]], T]) -> T:
        self.check()
        result = call(self.check)
        self.check()
        return result


async def _worker[T](call: Callable[[], T]) -> T:
    # Starlette/AnyIO request cancel scopes must not abandon an owned DB write.
    with anyio.CancelScope(shield=True):
        result = await run_in_threadpool(call)
    return result


@dataclass(frozen=True)
class Delivery:
    """A server callback that loads and authorizes content at dispatch time.

    Callback must check its resource's tenant/device/session scope using binding,
    and perform all reads/writes via tx, without external effects (retries occur).
    Never enqueue an already-authorized private body from an earlier watch event.
    """

    kind: str
    load: Callable[[Transaction, ConnectionBinding], dict[str, Any] | None]
    completed: Callable[[bool], None] | None = None


class DeviceConnection:
    def __init__(
        self, gateway: DeviceGateway, socket: WebSocket, binding: ConnectionBinding
    ) -> None:
        self.binding = binding
        self._gateway, self._socket = gateway, socket
        self._queue: asyncio.Queue[Delivery] = asyncio.Queue(QUEUE_SIZE)
        self._send_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[Any]] = set()
        self.closed = False
        self._overflow = asyncio.Event()
        self._failure_code = 1013

    def fail(self, code: int = 1011) -> None:
        """A trusted watch/task failure closes the connection through owned cleanup."""
        self._failure_code = code
        self._overflow.set()

    async def io[T](self, call: Callable[[], T]) -> T:
        return await self._io(lambda admit: call())

    async def _io[T](self, call: Callable[[Callable[[], None]], T]) -> T:
        # Queue admission expires independently of the shielded worker lifetime.
        # Already-started SDK calls remain owned and are drained before lease CAS.
        admission = _Admission(lambda: not self.closed, monotonic() + IO_TIMEOUT)
        task = asyncio.create_task(_worker(lambda: admission.invoke(call)))
        self._pending.add(task)
        try:
            return await asyncio.wait_for(asyncio.shield(task), IO_TIMEOUT)
        except BaseException:
            admission.cancelled.set()
            raise
        finally:
            if task.done():
                if not task.cancelled():
                    task.exception()
                self._pending.discard(task)

    async def run[T](self, work: Callable[[Transaction, ConnectionBinding], T]) -> T:
        if self.closed:
            raise ProtocolError()
        return await self._io(
            lambda admit: self._gateway.leases.run(
                self.binding, lambda tx: work(tx, self.binding), admit=admit
            )
        )

    def enqueue(self, delivery: Delivery) -> None:
        """Call on the event loop; server watch adapters must marshal their callbacks."""
        if self.closed or delivery.kind in {"hello_ack", "heartbeat_ack"}:
            raise ProtocolError()
        try:
            self._queue.put_nowait(delivery)
        except asyncio.QueueFull:
            self._overflow.set()
            raise ProtocolError(1013) from None

    async def send(self, kind: str, payload: dict[str, Any]) -> None:
        await self._transmit(Delivery(kind, lambda tx, binding: payload))

    async def enqueue_wait(self, delivery: Delivery) -> None:
        """Bounded, cancellation-cooperative backpressure for an owned watch pump."""
        if self.closed or delivery.kind in {"hello_ack", "heartbeat_ack"}:
            raise ProtocolError()
        await self._queue.put(delivery)

    async def _transmit(self, delivery: Delivery) -> None:
        async with self._send_lock:

            def prepare(tx: Transaction) -> Any:
                payload = delivery.load(tx, self.binding)
                if payload is None:
                    return None, release_guard(tx)
                text = encode(
                    envelope(
                        delivery.kind,
                        payload,
                        self.binding.generation,
                        self._gateway.leases.now().isoformat(),
                    )
                )
                return text, release_guard(tx)

            text, guard = await self._io(
                lambda admit: self._gateway.leases.run(self.binding, prepare, admit=admit)
            )
            # Retain all resource/credential/lease deadlines through thread handoff.
            guard.finish()
            if text is None:
                if delivery.completed is not None:
                    delivery.completed(False)
                return
            if self.closed:
                raise ProtocolError()
            async with asyncio.timeout(SEND_TIMEOUT):
                await self._socket.send_text(text)
            if delivery.completed is not None:
                delivery.completed(True)

    async def deliver(self) -> None:
        while True:
            await self._transmit(await self._queue.get())

    async def overflow(self) -> None:
        await self._overflow.wait()
        raise ProtocolError(self._failure_code)

    async def monitor(self) -> None:
        while True:
            await self._gateway.sleep(HEARTBEAT_SECONDS)
            await self.io(lambda: self._gateway.leases.check(self.binding))

    async def receive(self) -> None:
        while True:
            frame = await receive_frame(self._socket)
            if frame.get("connection_generation") != self.binding.generation:
                raise ProtocolError()
            if frame["type"] != "heartbeat":
                handler = self._gateway.handlers.get(frame["type"])
                if handler is None:
                    raise ProtocolError()
                await self._transmit(handler(frame["payload"]))
                continue
            # The optional session ID is telemetry, never a watch selector or idle renewal.
            expires = await self._io(
                lambda admit: self._gateway.leases.heartbeat(self.binding, admit=admit)
            )
            await self.send(
                "heartbeat_ack",
                {
                    "lease_expires_at": expires.isoformat(),
                    "server_time": self._gateway.leases.now().isoformat(),
                },
            )

    async def drain(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
            self._pending.clear()
        while not self._queue.empty():
            self._queue.get_nowait()


DeliveryHook = Callable[[DeviceConnection], AbstractAsyncContextManager[None]]


async def receive_frame(socket: WebSocket) -> dict[str, Any]:
    message = await socket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    return decode(message.get("text"))


class DeviceGateway:
    def __init__(
        self,
        leases: DeviceLeases,
        *,
        hooks: Sequence[DeliveryHook] = (),
        handlers: dict[str, Callable[[dict[str, Any]], Delivery]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.leases, self.hooks, self.sleep = leases, tuple(hooks), sleep
        self.handlers = dict(handlers or {})

    @asynccontextmanager
    async def _hooks(self, connection: DeviceConnection) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for hook in self.hooks:
                # No hello capability, cursor, session or URL value selects a hook's scope.
                await connection.io(lambda: self.leases.check(connection.binding))
                await stack.enter_async_context(hook(connection))
            yield

    async def connected(self, connection: DeviceConnection) -> None:
        async with self._hooks(connection):
            await asyncio.Future[None]()

    async def serve(self, socket: WebSocket, device_id: str) -> None:
        identity: DeviceIdentity | None = None
        connection: DeviceConnection | None = None
        connection_id = str(uuid4())
        tasks: list[asyncio.Task[Any]] = []
        pending: list[asyncio.Task[Any]] = []
        close_code = 1000
        outcome = "closed"
        closed = Event()

        async def io[T](call: Callable[[Callable[[], None]], T]) -> T:
            admission = _Admission(lambda: not closed.is_set(), monotonic() + IO_TIMEOUT)
            task = asyncio.create_task(_worker(lambda: admission.invoke(call)))
            pending.append(task)
            try:
                return await asyncio.wait_for(asyncio.shield(task), IO_TIMEOUT)
            except BaseException:
                admission.cancelled.set()
                raise

        try:
            headers = socket.headers.getlist("authorization")
            protocols = socket.headers.getlist("sec-websocket-protocol")
            if (
                socket.scope.get("query_string")
                or socket.headers.getlist("cookie")
                or len(headers) != 1
                or protocols != [SUBPROTOCOL]
            ):
                raise ProtocolError()
            scheme, separator, token = headers[0].partition(" ")
            if (
                scheme.lower() != "bearer"
                or not separator
                or not token
                or len(token) > 16384
                or any(c.isspace() for c in token)
            ):
                raise ProtocolError()
            identity = await io(lambda admit: self.leases.credentials.authenticate(token))
            if device_id != identity.sub:
                raise ProtocolError()
            await socket.accept(subprotocol=SUBPROTOCOL)
            async with asyncio.timeout(HELLO_TIMEOUT):
                hello = await receive_frame(socket)
            if hello["type"] != "hello" or hello["payload"]["device_id"] != identity.sub:
                raise ProtocolError()

            async def early_frame() -> None:
                # Observe peer disconnect while acquire waits for worker capacity.
                # Before hello_ack, another application frame is a protocol error.
                message = await socket.receive()
                closed.set()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))
                raise ProtocolError()

            peer = asyncio.create_task(early_frame())
            acquisition = asyncio.create_task(
                io(lambda admit: self.leases.acquire(identity, connection_id, admit=admit))
            )
            tasks = [peer, acquisition]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if peer in done:
                peer.result()
            binding = acquisition.result()
            peer.cancel()
            await asyncio.gather(peer, return_exceptions=True)
            connection = DeviceConnection(self, socket, binding)
            expires = await connection.io(lambda: self.leases.check(binding))
            await connection.send(
                "hello_ack",
                {
                    "heartbeat_interval_seconds": HEARTBEAT_SECONDS,
                    "lease_expires_at": expires.isoformat(),
                    "server_time": self.leases.now().isoformat(),
                    "snapshot_required": True,
                },
            )
            tasks = [
                asyncio.create_task(connection.receive()),
                asyncio.create_task(connection.monitor()),
                asyncio.create_task(connection.deliver()),
                asyncio.create_task(connection.overflow()),
                asyncio.create_task(self.connected(connection)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        except ProtocolError as error:
            close_code, outcome = error.code, "refused"
        except APIError:
            close_code, outcome = 1008, "unauthorized"
        except TimeoutError:
            close_code, outcome = 1013, "timeout"
        except asyncio.CancelledError:
            close_code, outcome = 1001, "cancelled"
            raise
        except Exception:
            close_code, outcome = 1011, "failed"
        finally:
            closed.set()
            if connection is not None:
                connection.closed = True
            # Close before waiting for any storage calls / hook finalizers.
            try:
                async with asyncio.timeout(SEND_TIMEOUT):
                    await socket.close(code=close_code)
            except (Exception, asyncio.CancelledError):
                pass

            async def cleanup() -> None:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.gather(*pending, return_exceptions=True)
                if connection is not None:
                    await connection.drain()
                if identity is not None:
                    try:
                        await run_in_threadpool(self.leases.release, identity, connection_id)
                    except Exception:
                        logger.warning('{"event":"device_lease_cleanup_failed"}')
                logger.info('{"event":"device_connection_closed","outcome":"%s"}', outcome)

            # Keep all finalizers owned even when the request is cancelled.
            with anyio.CancelScope(shield=True):
                cleanup_task = asyncio.create_task(cleanup())
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                    raise


@router.websocket("/devices/{device_id}/connect")
async def connect_device(socket: WebSocket, device_id: str) -> None:
    services = getattr(socket.app.state, "auth_services", None)
    gateway: DeviceGateway | None = getattr(services, "device_gateway", None)
    if gateway is None:
        await socket.close(code=1013)
        return
    await gateway.serve(socket, device_id)

"""Runtime-owned WSS connection; snapshot interpretation/execution remain separate."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.typing import Subprotocol

from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import MAX_BYTES, SUBPROTOCOL, encode, frame

QUIET = logging.Logger("brainoid.device.wire", level=logging.CRITICAL + 1)
QUIET.propagate = False


class NoRedirect(connect):
    """websockets otherwise follows HTTP redirects, potentially forwarding Authorization."""

    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def backoff(attempt: int, jitter: Callable[[], float] = random.random) -> float:
    return float(min(30.0, max(1.0, 2 ** min(attempt, 5) * (0.5 + jitter() / 2))))


def planned_reconnect(jitter: Callable[[], float] = random.random) -> float:
    return 3240 + 120 * jitter()


class DeviceTransport:
    def __init__(
        self,
        client: CloudClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.client, self.monotonic, self.jitter = client, monotonic, jitter
        self.generation = 0
        self.socket: ClientConnection | None = None
        self.accepted = False

    def _frame(self, kind: str, payload: dict[str, Any]) -> str:
        value: dict[str, Any] = {
            "protocol_version": 1,
            "message_id": str(uuid4()),
            "type": kind,
            "sent_at": datetime.fromtimestamp(self.client.now(), UTC).isoformat(),
            "payload": payload,
        }
        if kind != "hello":
            value["connection_generation"] = self.generation
        return encode(value).decode()

    async def connection(self, token: str) -> None:
        client = self.client
        self.accepted = False
        credentials = client.journal.credentials
        assert credentials is not None
        binding = client.storage.snapshot().registration
        if binding is None:
            raise CloudError("cloud_stopped", 409)
        generation = client.generation
        deadline = self.monotonic() + min(
            planned_reconnect(self.jitter), max(0, client.journal.access_until - client.now() - 30)
        )
        url = client.settings.api_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        async with NoRedirect(
            url + f"/v1/devices/{credentials.device_id}/connect",
            additional_headers={"Authorization": "Bearer " + token},
            subprotocols=[Subprotocol(SUBPROTOCOL)],
            proxy=None,
            compression=None,
            open_timeout=10,
            close_timeout=1,
            ping_interval=None,
            max_size=MAX_BYTES,
            max_queue=4,
            logger=QUIET,
        ) as socket:
            self.socket = socket
            try:
                client.check(generation)
                if socket.subprotocol != SUBPROTOCOL:
                    raise CloudError("invalid_cloud_response", 502)
                async with asyncio.timeout(10):
                    await socket.send(
                        self._frame(
                            "hello",
                            {
                                "device_id": credentials.device_id,
                                "software_version": "0.1.0",
                                "supported_protocol_versions": [1],
                                "capabilities": [],
                                "last_received_seq": binding.delivery_cursor,
                            },
                        )
                    )
                    ack = frame(await socket.recv())
                client.check(generation)
                if (
                    ack["type"] != "hello_ack"
                    or ack["payload"]["heartbeat_interval_seconds"] != 15
                    or ack["connection_generation"] <= binding.connection_generation
                ):
                    raise CloudError("invalid_cloud_response", 502)
                self.generation = ack["connection_generation"]
                client.storage.set_generation(binding.epoch, self.generation)
                client.status = "connected"
                self.accepted = True
                heartbeat = self.monotonic() + 15
                last_ack = self.monotonic()
                while self.monotonic() < deadline:
                    client.check(generation)
                    now = self.monotonic()
                    if now - last_ack >= 45:
                        raise CloudError()
                    if now >= heartbeat:
                        async with asyncio.timeout(5):
                            await socket.send(
                                self._frame(
                                    "heartbeat", {"last_received_seq": binding.delivery_cursor}
                                )
                            )
                        heartbeat = now + 15
                    try:
                        async with asyncio.timeout(
                            max(0.001, min(heartbeat, deadline, last_ack + 45) - self.monotonic())
                        ):
                            value = frame(await socket.recv())
                    except TimeoutError:
                        continue
                    client.check(generation)
                    if value.get("connection_generation") != self.generation or value["type"] in {
                        "hello",
                        "hello_ack",
                        "heartbeat",
                        "command_start",
                        "command_status",
                        "notification_ack",
                    }:
                        raise CloudError("invalid_cloud_response", 502)
                    if value["type"] == "heartbeat_ack":
                        last_ack = self.monotonic()
                    elif value["type"] in {"session_revoked", "error"}:
                        # Until DEV04 owns per-session semantics, fail closed and
                        # discard all private output rather than interpreting a snapshot.
                        client.stop()
                        raise CloudError("authorization_refused", 403)
                    # Valid command/session/run/notification frames have no consumer
                    # yet. Never ACK, advance a cursor or execute an appliance here.
            finally:
                self.socket = None
                if client.active:
                    client.status = "connecting"

    async def run(self) -> None:
        attempt = 0
        rejected: str | None = None
        rejected_version: int | None = None
        while self.client.active:
            try:
                if self.client.journal.credentials is None:
                    await self.client.sleep(1)
                    continue
                token = await self.client.token(
                    rejected=rejected, rejected_version=rejected_version
                )
                version = self.client.journal.credential_version
                try:
                    await self.connection(token)
                except InvalidStatus as error:
                    if error.response.status_code in {401, 403}:
                        if rejected is not None:
                            self.client.stop()
                            return
                        rejected = token
                        rejected_version = version
                        continue
                    raise
                except ConnectionClosed as error:
                    if error.rcvd and error.rcvd.code == 1008:
                        if rejected is not None and not self.accepted:
                            self.client.stop()
                            return
                        rejected = token
                        rejected_version = version
                        continue
                    raise
                attempt, rejected = 0, None
            except CloudError as error:
                if error.code != "cloud_unavailable":
                    if self.client.active:
                        self.client.stop()
                    return
            except (WebSocketException, OSError, TimeoutError):
                pass
            if self.client.active:
                await self.client.sleep(backoff(attempt, self.jitter))
                attempt = min(attempt + 1, 5)

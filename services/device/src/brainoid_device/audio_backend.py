"""Memory-only ALSA IPC. Native calls are isolated in Runtime-owned children."""

import asyncio
import os
import re
import secrets
import socket
import struct
import sys
from pathlib import Path
from typing import Protocol

from brainoid_device.cloud_models import CloudError
from brainoid_device.cloud_wire import decode
from brainoid_device.owned_child import OwnedChild
from brainoid_device.runtime import Runtime

DEVICE = re.compile(r"(?:plug)?hw:CARD=[A-Za-z0-9_]+,DEV=[0-9]{1,2}")
BLOCK = 3200
LAUNCH = """
import sys
sys.path.insert(0, sys.argv.pop(1))
from brainoid_device.audio_native import main
raise SystemExit(main())
"""


class AudioStream(Protocol):
    async def read(self) -> bytes: ...
    async def write(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...
    def abort(self) -> None: ...


class AudioBackend(Protocol):
    async def devices(self) -> list[dict[str, str]]: ...
    async def open(self, device: str, capture: bool) -> AudioStream: ...


class NativeStream:
    def __init__(self, connection: socket.socket, child: OwnedChild) -> None:
        self.connection, self.child = connection, child
        self.closed = False

    async def read(self) -> bytes:
        return await asyncio.get_running_loop().sock_recv(self.connection, BLOCK)

    async def write(self, pcm: bytes) -> None:
        await asyncio.get_running_loop().sock_sendall(self.connection, pcm)

    async def finish(self) -> None:
        self.connection.shutdown(socket.SHUT_WR)
        # The helper drains the hardware then returns a one-byte success receipt.
        if await self.read() != b"!":
            raise CloudError("audio_device_failed", 503)

    def abort(self) -> None:
        if not self.closed:
            self.closed = True
            self.connection.close()
            if not self.child.drained():
                # Guardian escalates to KILL after its bounded TERM window.
                self.child.terminate()


class AlsaBackend:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._children: list[OwnedChild] = []
        self._opening = 0

    async def connect(self, mode: str, device: str = "") -> NativeStream:
        self._children = [child for child in self._children if not child.drained()]
        if len(self._children) + self._opening >= 4:
            raise CloudError("audio_busy", 429)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._opening += 1
        listener.setblocking(False)
        address = "brainoid-audio-" + secrets.token_hex(24)
        child: OwnedChild | None = None
        connection: socket.socket | None = None
        try:
            listener.bind("\0" + address)
            listener.listen(4)
            async with asyncio.timeout(3):
                child = await self.runtime.start_child(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        "-c",
                        LAUNCH,
                        str(Path(__file__).resolve().parents[1]),
                        address,
                        str(os.getpid()),
                        mode,
                        device,
                    ]
                )
                self._children.append(child)
                while True:
                    connection, _ = await asyncio.get_running_loop().sock_accept(listener)
                    pid, uid, _ = struct.unpack(
                        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    )
                    try:
                        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                        valid = uid == os.getuid() and int(stat[1]) == child.pid
                    except (OSError, ValueError, IndexError):
                        valid = False
                    if valid:
                        break
                    connection.close()
                    connection = None
                stream = NativeStream(connection, child)
                if await stream.read() != b"!":
                    raise CloudError("audio_device_failed", 503)
                connection = None
                child = None
                return stream
        except (OSError, TimeoutError):
            raise CloudError("audio_device_unavailable", 503) from None
        finally:
            self._opening -= 1
            listener.close()
            if connection is not None:
                connection.close()
            if child is not None and not child.drained():
                child.terminate()

    async def devices(self) -> list[dict[str, str]]:
        stream = await self.connect("list")
        try:
            async with asyncio.timeout(3):
                # Handshake prevents the receipt and JSON coalescing into one read.
                await stream.write(b"!")
                data = bytearray()
                while chunk := await stream.read():
                    data.extend(chunk)
                    if len(data) > 32768:
                        raise CloudError("audio_device_failed", 503)
                value = decode(bytes(data)).get("devices")
                if not isinstance(value, list) or len(value) > 64:
                    raise CloudError("audio_device_failed", 503)
                result: list[dict[str, str]] = []
                for item in value:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"id", "direction"}
                        or not isinstance(item["id"], str)
                        or not DEVICE.fullmatch(item["id"])
                        or item["direction"] not in {"input", "output"}
                    ):
                        raise CloudError("audio_device_failed", 503)
                    result.append(item)
                return result
        finally:
            stream.abort()

    async def open(self, device: str, capture: bool) -> AudioStream:
        if not DEVICE.fullmatch(device):
            raise CloudError("audio_device_invalid", 400)
        stream = await self.connect("capture" if capture else "playback", device)
        try:
            await stream.write(b"!")
            return stream
        except BaseException:
            stream.abort()
            raise

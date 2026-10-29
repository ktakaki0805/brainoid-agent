"""Trusted child: ALSA ctypes calls, no files, logs or cloud credentials on IPC."""

import ctypes as c
import json
import os
import socket
import struct
import sys
import time
from typing import Any

from brainoid_device.audio_backend import BLOCK, DEVICE


def library() -> c.CDLL:
    lib = c.CDLL("libasound.so.2")
    signatures: dict[str, tuple[Any, list[Any]]] = {
        "snd_pcm_open": (c.c_int, [c.POINTER(c.c_void_p), c.c_char_p, c.c_int, c.c_int]),
        "snd_pcm_set_params": (
            c.c_int,
            [c.c_void_p, c.c_int, c.c_int, c.c_uint, c.c_uint, c.c_int, c.c_uint],
        ),
        "snd_pcm_readi": (c.c_long, [c.c_void_p, c.c_void_p, c.c_ulong]),
        "snd_pcm_writei": (c.c_long, [c.c_void_p, c.c_void_p, c.c_ulong]),
        "snd_pcm_drain": (c.c_int, [c.c_void_p]),
        "snd_pcm_drop": (c.c_int, [c.c_void_p]),
        "snd_pcm_close": (c.c_int, [c.c_void_p]),
        "snd_device_name_hint": (c.c_int, [c.c_int, c.c_char_p, c.POINTER(c.c_void_p)]),
        "snd_device_name_get_hint": (c.c_void_p, [c.c_void_p, c.c_char_p]),
        "snd_device_name_free_hint": (c.c_int, [c.c_void_p]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    return lib


def devices(lib: c.CDLL) -> list[dict[str, str]]:
    hints = c.c_void_p()
    if lib.snd_device_name_hint(-1, b"pcm", c.byref(hints)) < 0:
        raise RuntimeError
    libc = c.CDLL(None)
    libc.free.argtypes = [c.c_void_p]
    result: list[dict[str, str]] = []
    try:
        pointers = c.cast(hints, c.POINTER(c.c_void_p))
        for index in range(256):
            if not pointers[index]:
                break
            values: list[str] = []
            for key in (b"NAME", b"IOID"):
                pointer = lib.snd_device_name_get_hint(pointers[index], key)
                try:
                    values.append(c.string_at(pointer).decode() if pointer else "")
                finally:
                    libc.free(pointer)
            name, direction = values
            if DEVICE.fullmatch(name):
                for kind in ("input", "output"):
                    if not direction or direction.lower() == kind:
                        item = {"id": name, "direction": kind}
                        if item not in result:
                            result.append(item)
            if len(result) > 64:
                raise RuntimeError
    finally:
        lib.snd_device_name_free_hint(hints)
    return result


def transfer(lib: c.CDLL, connection: socket.socket, mode: str, device: str) -> None:
    if not DEVICE.fullmatch(device):
        raise RuntimeError
    pcm = c.c_void_p()
    capture = mode == "capture"
    # NONBLOCK; S16_LE=2, RW_INTERLEAVED=3. No fallback to a default microphone.
    if lib.snd_pcm_open(c.byref(pcm), device.encode(), int(capture), 1) < 0:
        raise RuntimeError
    try:
        # Conversion is an explicit choice of plughw, never a fallback from hw.
        resample = int(device.startswith("plughw:"))
        if lib.snd_pcm_set_params(pcm, 2, 3, 1, 16000, resample, 100000) < 0:
            raise RuntimeError
        connection.sendall(b"!")
        # Explicit ack: no PCM can coalesce with the open receipt.
        if connection.recv(1) != b"!":
            return
        buffer = c.create_string_buffer(BLOCK)
        frames = 0
        deadline = time.monotonic() + 65
        while (not capture or frames < 960000) and time.monotonic() < deadline:
            if capture:
                count = int(lib.snd_pcm_readi(pcm, buffer, min(BLOCK // 2, 960000 - frames)))
                if count == -11:
                    time.sleep(0.01)
                    continue
                if count <= 0:
                    raise RuntimeError
                connection.sendall(buffer.raw[: count * 2])
                frames += count
            else:
                data = connection.recv(BLOCK)
                if not data:
                    while (status := lib.snd_pcm_drain(pcm)) == -11:
                        if time.monotonic() >= deadline:
                            raise RuntimeError
                        time.sleep(0.01)
                    if status < 0:
                        raise RuntimeError
                    connection.sendall(b"!")
                    return
                # SOCK_STREAM may split a 16-bit sample.
                if len(data) % 2:
                    data += connection.recv(1)
                if len(data) % 2 or frames + len(data) // 2 > 960000:
                    raise RuntimeError
                offset = 0
                while offset < len(data):
                    if time.monotonic() >= deadline:
                        raise RuntimeError
                    count = int(lib.snd_pcm_writei(pcm, data[offset:], (len(data) - offset) // 2))
                    if count == -11:
                        time.sleep(0.01)
                        continue
                    if count <= 0:
                        raise RuntimeError
                    offset += count * 2
                    frames += count
    finally:
        lib.snd_pcm_drop(pcm)
        lib.snd_pcm_close(pcm)


def main() -> int:
    address, parent, mode, device = sys.argv[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect("\0" + address)
            pid, uid, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if pid != int(parent) or uid != os.getuid():
                return 1
            lib = library()
            if mode == "list":
                result = devices(lib)
                connection.sendall(b"!")
                if connection.recv(1) == b"!":
                    connection.sendall(json.dumps({"devices": result}).encode())
            elif mode in {"capture", "playback"}:
                transfer(lib, connection, mode, device)
            else:
                return 1
        return 0
    except Exception:
        return 1  # stderr is DEVNULL; never disclose ALSA/provider payloads.

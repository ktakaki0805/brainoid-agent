"""ALSA transfer contracts and real 48 kHz synthetic null conversion, no hardware."""

import asyncio
import ctypes as c
import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock, patch

from brainoid_device.audio_backend import BLOCK, AlsaBackend
from brainoid_device.audio_capture import pcm_from_wav, wav
from brainoid_device.audio_native import transfer
from brainoid_device.cloud_models import CloudError
from brainoid_device.runtime import Runtime


class NativeTransferTests(unittest.TestCase):
    def library(self) -> Mock:
        lib = Mock()
        for method in (
            "snd_pcm_open",
            "snd_pcm_set_params",
            "snd_pcm_drain",
            "snd_pcm_drop",
            "snd_pcm_close",
        ):
            getattr(lib, method).return_value = 0
        return lib

    def test_split_samples_short_writes_eagain_and_drain_preserve_pcm(self) -> None:
        lib, connection = self.library(), Mock()
        connection.recv.side_effect = [b"!", b"\x01", b"\x00", b"\x02\x00\x03\x00", b""]
        counts = iter([-11, 1, 1, -11, 1])
        output = bytearray()

        def write(pcm: object, data: bytes, frames: int) -> int:
            count = next(counts)
            self.assertEqual(len(data), frames * 2)
            if count > 0:
                output.extend(data[: count * 2])
            return count

        lib.snd_pcm_writei.side_effect = write
        lib.snd_pcm_drain.side_effect = [-11, 0]
        transfer(
            cast(c.CDLL, lib),
            cast(socket.socket, connection),
            "playback",
            "plughw:CARD=Synthetic,DEV=0",
        )
        self.assertEqual(output, b"\x01\x00\x02\x00\x03\x00")
        self.assertEqual(pcm_from_wav(wav(output)), output)
        self.assertEqual(lib.snd_pcm_set_params.call_args.args[1:], (2, 3, 1, 16000, 1, 100000))
        self.assertEqual(connection.sendall.call_args_list, [((b"!",),), ((b"!",),)])
        lib.snd_pcm_drop.assert_called_once()
        lib.snd_pcm_close.assert_called_once()

    def test_short_capture_and_eagain_then_unplug_drop_close_without_fallback(self) -> None:
        lib, connection = self.library(), Mock()
        connection.recv.return_value = b"!"
        counts = iter([-11, 2, -19])

        def read(pcm: object, buffer: Any, frames: int) -> int:
            count = next(counts)
            self.assertEqual(frames, BLOCK // 2)
            if count > 0:
                c.memmove(buffer, b"\x01\x00\x02\x00", count * 2)
            return count

        lib.snd_pcm_readi.side_effect = read
        with self.assertRaises(RuntimeError):
            transfer(
                cast(c.CDLL, lib),
                cast(socket.socket, connection),
                "capture",
                "hw:CARD=Synthetic,DEV=0",
            )
        self.assertEqual(connection.sendall.call_args_list, [((b"!",),), ((b"\x01\0\x02\0",),)])
        self.assertEqual(lib.snd_pcm_set_params.call_args.args[1:], (2, 3, 1, 16000, 0, 100000))
        self.assertEqual(lib.snd_pcm_open.call_count, 1)
        lib.snd_pcm_drop.assert_called_once()
        lib.snd_pcm_close.assert_called_once()

    def test_drain_failure_or_partial_sample_never_reports_completion(self) -> None:
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                lib, connection = self.library(), Mock()
                connection.recv.side_effect = [b"!", b"x", b""] if malformed else [b"!", b""]
                lib.snd_pcm_drain.return_value = -32
                with self.assertRaises(RuntimeError):
                    transfer(
                        cast(c.CDLL, lib),
                        cast(socket.socket, connection),
                        "playback",
                        "hw:CARD=Synthetic,DEV=0",
                    )
                connection.sendall.assert_called_once_with(b"!")
                lib.snd_pcm_close.assert_called_once()


class RealNullConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_plughw_converts_16k_pcm_to_48k_null_slave(self) -> None:
        # Native calls run in the real owned helper. Config contains no audio;
        # both named PCMs are synthetic, with no physical device fallback.
        with tempfile.TemporaryDirectory(prefix="ba-voice-04-writer-r1fix-null-") as directory:
            config = Path(directory) / "alsa.conf"
            config.write_text(
                "\n".join(
                    "pcm."
                    + name
                    + """ {
 @args [ CARD DEV ]
 @args.CARD { type string }
 @args.DEV { type integer }
 type plug
 slave { pcm { type null } rate 48000 }
}
"""
                    for name in ("hw", "plughw")
                )
            )
            runtime = Runtime(0.5)
            runtime.running = True
            backend = AlsaBackend(runtime)
            with patch.dict(os.environ, {"ALSA_CONFIG_PATH": str(config)}):
                try:
                    with self.assertRaises(CloudError):
                        await backend.open("hw:CARD=Synthetic,DEV=0", False)
                    stream = await backend.open("plughw:CARD=Synthetic,DEV=0", False)
                    try:
                        # 1,600 frames at the explicit client rate of 16 kHz.
                        await stream.write(b"\x01\x00" * 1600)
                        async with asyncio.timeout(3):
                            await stream.finish()
                    finally:
                        stream.abort()
                    capture = await backend.open("plughw:CARD=Synthetic,DEV=0", True)
                    try:
                        async with asyncio.timeout(3):
                            block = await capture.read()
                        self.assertTrue(0 < len(block) <= BLOCK)
                    finally:
                        capture.abort()
                    with self.assertRaises(CloudError):
                        await backend.open("default", False)
                finally:
                    await runtime.close()
            self.assertEqual(runtime._children, [])
            self.assertFalse(runtime.resource_failed)

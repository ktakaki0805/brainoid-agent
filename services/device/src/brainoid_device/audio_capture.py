"""Bounded 16 kHz signed little-endian mono PCM/WAV, held only in memory."""

import struct

from brainoid_device.cloud_models import CloudError

MAX_PCM = 60 * 16000 * 2
MAX_WAV = 5 * 1024 * 1024


def wav(pcm: bytes | bytearray) -> bytes:
    if not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM or not any(pcm):
        raise CloudError("audio_silent_or_invalid", 422)
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        16000,
        32000,
        2,
        16,
        b"data",
        len(pcm),
    ) + bytes(pcm)


def pcm_from_wav(audio: bytes) -> bytes:
    """Same complete RIFF extent and PCM constraints as the cloud contract."""

    def invalid() -> CloudError:
        return CloudError("audio_invalid_response", 502)

    if (
        not 12 <= len(audio) <= MAX_WAV
        or audio[:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
        or struct.unpack_from("<I", audio, 4)[0] != len(audio) - 8
    ):
        raise invalid()
    offset, chunks = 12, 0
    seen_format = False
    pcm = None
    while offset < len(audio):
        chunks += 1
        if chunks > 1024 or offset + 8 > len(audio):
            raise invalid()
        kind, size = struct.unpack_from("<4sI", audio, offset)
        start, end = offset + 8, offset + 8 + size
        if end + size % 2 > len(audio):
            raise invalid()
        if kind == b"fmt ":
            if (
                seen_format
                or pcm is not None
                or size not in (16, 18)
                or struct.unpack_from("<HHIIHH", audio, start) != (1, 1, 16000, 32000, 2, 16)
                or (size == 18 and audio[end - 2 : end] != b"\0\0")
            ):
                raise invalid()
            seen_format = True
        elif kind == b"data":
            if pcm is not None or not seen_format or not 0 < size <= MAX_PCM or size % 2:
                raise invalid()
            pcm = audio[start:end]
        offset = end + size % 2
    if pcm is None or not any(pcm):
        raise invalid()
    return pcm


def erase(buffer: bytearray) -> None:
    buffer[:] = b"\0" * len(buffer)
    buffer.clear()

"""Bounded strict decoding for untrusted JSON, SSE and the installed WSS schema."""

import codecs
import json
import math
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from brainoid_device.cloud_models import CloudError

MAX_BYTES = 65536
SUBPROTOCOL = "brainoid-agent.device.v1"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CloudError("invalid_cloud_response", 502)
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def decode(raw: str | bytes) -> dict[str, Any]:
    try:
        if len(raw.encode() if isinstance(raw, str) else raw) > MAX_BYTES:
            raise ValueError
        result = json.loads(
            raw, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise CloudError("invalid_cloud_response", 502) from None


def encode(value: object) -> bytes:
    try:
        result = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(result) > MAX_BYTES:
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise CloudError("invalid_cloud_response", 502) from None


CHECKER = FormatChecker()
PROTOCOL = json.loads(files("brainoid_device").joinpath("device_protocol.schema.json").read_text())
EVENTS = json.loads(files("brainoid_device").joinpath("run_events.schema.json").read_text())
WS_VALIDATOR = Draft202012Validator(PROTOCOL, format_checker=CHECKER)
EVENT_VALIDATOR = Draft202012Validator(EVENTS, format_checker=CHECKER)


def frame(raw: str | bytes) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise CloudError("invalid_cloud_response", 502)
    value = decode(raw)
    if not WS_VALIDATOR.is_valid(value):
        raise CloudError("invalid_cloud_response", 502)
    return value


class SSEDecoder:
    """Incremental UTF-8, LF/CRLF parser, bounded even without a final newline."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._buffer = ""
        self._lines: list[str] = []
        self._size = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        events = []
        offset = 0
        while offset < len(chunk):
            newline = chunk.find(b"\n", offset)
            end = len(chunk) if newline < 0 else newline + 1
            self._size += end - offset
            if self._size > MAX_BYTES:
                raise CloudError("invalid_cloud_response", 502)
            try:
                self._buffer += self._decoder.decode(chunk[offset:end])
            except UnicodeError:
                raise CloudError("invalid_cloud_response", 502) from None
            offset = end
            if newline >= 0:
                line, self._buffer = self._buffer[:-1].removesuffix("\r"), ""
                if line:
                    self._lines.append(line)
                else:
                    event = self._event()
                    self._lines, self._size = [], 0
                    if event is not None:
                        events.append(event)
        return events

    def _event(self) -> dict[str, Any] | None:
        values: dict[str, str] = {}
        data_lines: list[str] = []
        for line in self._lines:
            if line.startswith(":"):
                continue
            name, separator, value = line.partition(":")
            if not separator or name not in {"id", "event", "data"}:
                raise CloudError("invalid_cloud_response", 502)
            if name == "data":
                data_lines.append(value.removeprefix(" "))
            elif name in values:
                raise CloudError("invalid_cloud_response", 502)
            else:
                values[name] = value.removeprefix(" ")
        if data_lines:
            values["data"] = "\n".join(data_lines)
        if not values:
            return None
        if set(values) != {"id", "event", "data"}:
            raise CloudError("invalid_cloud_response", 502)
        data = decode(values["data"])
        if (
            not EVENT_VALIDATOR.is_valid(data)
            or str(data["seq"]) != values["id"]
            or data["type"] != values["event"]
        ):
            raise CloudError("invalid_cloud_response", 502)
        return data

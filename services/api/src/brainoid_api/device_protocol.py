"""Bounded strict JSON and the packaged v1 wire contract (parity-tested with specs)."""

import json
import math
from importlib.resources import files
from typing import Any
from uuid import uuid4

from brainoid_contracts import normalize_utc
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

MAX_FRAME_BYTES = 64 * 1024
SUBPROTOCOL = "brainoid-agent.device.v1"
SCHEMA = json.loads(files("brainoid_api").joinpath("device_protocol.schema.json").read_text())


def _timestamp(value: object) -> bool:
    if isinstance(value, str):
        normalize_utc(value)
    return True


CHECKER = FormatChecker()
CHECKER.checks("date-time", raises=ValueError)(_timestamp)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=CHECKER)
SERVER_TYPES = frozenset(SCHEMA["$defs"]) - {
    "hello",
    "heartbeat",
    "command_status",
    "command_start",
    "notification_ack",
    "HomeAction",
}


class ProtocolError(Exception):
    def __init__(self, code: int = 1008) -> None:
        self.code = code
        super().__init__("device_protocol_error")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError()
        result[key] = value
    return result


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolError()
    return result


def _constant(value: str) -> None:
    raise ProtocolError()


def validate(frame: Any) -> dict[str, Any]:
    if not isinstance(frame, dict) or not VALIDATOR.is_valid(frame):
        raise ProtocolError()
    return frame


def decode(text: str | None) -> dict[str, Any]:
    if text is None:
        raise ProtocolError(1003)
    try:
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError(1009)
        frame = json.loads(
            text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
        )
        return validate(frame)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError() from None


def encode(frame: dict[str, Any]) -> str:
    validate(frame)
    try:
        text = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError(1009)
        return text
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError() from None


def envelope(kind: str, payload: dict[str, Any], generation: int, now: str) -> dict[str, Any]:
    if kind not in SERVER_TYPES:
        raise ProtocolError()
    return {
        "protocol_version": 1,
        "message_id": str(uuid4()),
        "type": kind,
        "sent_at": now,
        "connection_generation": generation,
        "payload": payload,
    }

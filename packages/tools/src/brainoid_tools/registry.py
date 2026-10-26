"""Strict canonical schema validation followed by a registered business argument type."""

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal, Protocol

from brainoid_agent_core.context import require_json
from brainoid_agent_core.ports import CallControl
from brainoid_agent_core.types import ToolCall, ToolDefinition
from brainoid_contracts import IanaTimezone, ResourceId, TenantContext, normalize_utc
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, ValidationError

from brainoid_tools.results import HandlerResult, ToolRejected


class Arguments(BaseModel):
    """Every handler defines its own narrow fields; never accepts arbitrary model JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


@dataclass(frozen=True)
class Invocation[A: Arguments]:
    context: TenantContext
    arguments: A
    tool_call_id: ResourceId
    effect_id: ResourceId
    confirmation: Literal["not_required", "confirmed"]


class Handler[A: Arguments](Protocol):
    async def execute(self, invocation: Invocation[A], *, control: CallControl) -> HandlerResult:
        """Outside DB transactions. Map typed fields to a narrow business API."""
        ...

    async def reconcile(self, invocation: Invocation[A], *, control: CallControl) -> HandlerResult:
        """Read provider/business state by effect_id; MUST NOT issue the original effect."""
        ...


@dataclass(frozen=True)
class Registration[A: Arguments]:
    name: str
    arguments_type: type[A]
    handler: Handler[A]
    effect_kind: Literal["operation", "command"] = "operation"


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return True
    normalize_utc(value)
    return True


def _timezone(value: object) -> bool:
    if not isinstance(value, str):
        return True
    IanaTimezone(value)
    return True


class Registry:
    def __init__(self) -> None:
        schema = json.loads(
            files("brainoid_tools").joinpath("tool-contracts.schema.json").read_text()
        )
        checker = FormatChecker()
        checker.checks("date-time", raises=ValueError)(_timestamp)
        checker.checks("iana-timezone", raises=ValueError)(_timezone)
        self._validator = Draft202012Validator(schema, format_checker=checker)
        self._names = frozenset(
            schema["$defs"][entry["$ref"].split("/")[-1]]["properties"]["name"]["const"]
            for entry in schema["oneOf"]
        )
        self._registered: dict[str, Registration[Any]] = {}

    def copy(self) -> "Registry":
        result = Registry()
        result._registered = dict(self._registered)
        return result

    def register[A: Arguments](self, registration: Registration[A]) -> None:
        if registration.name not in self._names or registration.name in self._registered:
            raise ValueError("Tool is unknown or already registered")
        if (
            not issubclass(registration.arguments_type, Arguments)
            or registration.arguments_type.model_config.get("extra") != "forbid"
            or registration.arguments_type.model_config.get("strict") is not True
        ):
            raise ValueError("Arguments must forbid extra fields and coercion")
        self._registered[registration.name] = registration

    def definitions(self, allowed: tuple[str, ...]) -> tuple[ToolDefinition, ...]:
        return tuple(
            ToolDefinition(
                name=name,
                description=name,
                parameters=registration.arguments_type.model_json_schema(),
            )
            for name, registration in self._registered.items()
            if name in allowed
        )

    def validate(self, call: ToolCall) -> tuple[Registration[Any], Arguments, str]:
        if call.name not in self._registered:
            raise ToolRejected("invalid_arguments")
        registration = self._registered[call.name]
        try:
            if len(call.arguments_json.encode("utf-8")) > 65536:
                raise ValueError("Arguments exceed the input limit")
            value = json.loads(call.arguments_json, object_pairs_hook=_object)
            require_json(value)
            if not self._validator.is_valid({"name": call.name, "arguments": value}):
                raise ValueError("Invalid tool input")
            typed = registration.arguments_type.model_validate(value)
            canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError, ValidationError):
            raise ToolRejected("invalid_arguments") from None
        return registration, typed, canonical

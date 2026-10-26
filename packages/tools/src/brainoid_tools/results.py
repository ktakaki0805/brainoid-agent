"""Typed handler outcomes; exceptions and stop signals never prove an effect failed."""

from typing import Literal, Self

from brainoid_agent_core.context import require_json
from brainoid_agent_core.types import ToolResult
from brainoid_agent_core.types.common import CoreValue, JsonObject
from brainoid_contracts import ResourceId
from pydantic import Field, model_validator


class HandlerResult(CoreValue):
    status: Literal["succeeded", "failed", "waiting_confirmation", "outcome_unknown"]
    result: JsonObject = Field(default_factory=dict, repr=False)
    error_code: Literal["rejected", "not_found", "conflict", "unavailable"] | None = None
    proposal_id: ResourceId | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        require_json(self.result)
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("Only an explicit failure has an error code")
        if self.status == "waiting_confirmation" and self.proposal_id is None:
            raise ValueError("A durable proposal is required")
        return self


class ToolRejected(Exception):
    """Safe categories only; do not wrap model/provider exception messages."""

    def __init__(
        self,
        code: Literal["invalid_arguments", "permission_denied", "conflict", "rate_limited"],
    ) -> None:
        self.code = code
        super().__init__(code)


def correlate(
    value: HandlerResult,
    tool_call_id: ResourceId,
    identity: ResourceId,
    kind: Literal["operation", "command"],
) -> ToolResult:
    # Validate raw containers before invoking a serializer (including model_construct).
    require_json(value.result)
    if len(value.model_dump_json().encode("utf-8")) > 65536:
        raise ValueError("Tool result exceeds the storage limit")
    checked = HandlerResult.model_validate(value.model_dump()).model_copy(deep=True)
    return ToolResult(
        tool_call_id=tool_call_id,
        operation_id=identity if kind == "operation" else None,
        command_id=identity if kind == "command" else None,
        **checked.model_dump(),
    )

"""Trusted execution envelope separated from untrusted model arguments."""

from typing import Literal, Self

from brainoid_contracts import ResourceId, TenantContext
from pydantic import model_validator

from brainoid_agent_core.types.common import CoreValue, JsonObject, NonEmptyText, Stopped
from brainoid_agent_core.types.model import ToolCall


class ToolExecution(CoreValue):
    context: TenantContext
    tool_call_id: ResourceId
    idempotency_key: NonEmptyText
    call: ToolCall


class ToolResult(CoreValue):
    """Correlates durable effects; outcome_unknown must be reconciled before retrying."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: ResourceId
    status: Literal["succeeded", "failed", "waiting_confirmation", "outcome_unknown"]
    result: JsonObject
    error_code: Literal["rejected", "not_found", "conflict", "unavailable"] | None = None
    operation_id: ResourceId | None = None
    command_id: ResourceId | None = None
    proposal_id: ResourceId | None = None

    @model_validator(mode="after")
    def validate_correlation(self) -> Self:
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("Only explicit failure requires an error_code")
        if self.status == "waiting_confirmation" and self.proposal_id is None:
            raise ValueError("Confirmation requires a durable proposal_id")
        if (
            self.status == "outcome_unknown"
            and self.operation_id is None
            and self.command_id is None
        ):
            raise ValueError("An uncertain effect requires an operation_id or command_id")
        return self


type ToolExecutionResult = ToolResult | Stopped

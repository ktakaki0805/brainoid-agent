"""Shared values and normalized termination, independent of transport and provider SDKs."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

NonEmptyText = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
JsonObject = dict[str, JsonValue]


class CoreValue(BaseModel):
    """Validated boundary value. Adapters must snapshot nested JSON when retaining it."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Completed(CoreValue):
    type: Literal["completed"] = "completed"
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"] = "stop"


class Cancelled(CoreValue):
    """Stop future output; consumers also discard queued speech. No side-effect rollback."""

    type: Literal["cancelled"] = "cancelled"


class TimedOut(CoreValue):
    type: Literal["timed_out"] = "timed_out"


class Failed(CoreValue):
    """Safe normalized category, never a raw SDK exception or user/provider payload."""

    type: Literal["failed"] = "failed"
    code: Literal[
        "invalid_arguments",
        "permission_denied",
        "unavailable",
        "rate_limited",
        "stream_interrupted",
        "conflict",
        "not_found",
    ]
    retryable: bool = False


type Stopped = Cancelled | TimedOut | Failed
type TerminalEvent = Completed | Stopped

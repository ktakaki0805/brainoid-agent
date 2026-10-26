"""Integer units: tokens and rounded-up audio seconds, never prices or message content."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Count = Annotated[int, Field(strict=True, ge=0)]
Positive = Annotated[int, Field(strict=True, ge=1)]


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class Amounts(Value):
    input_tokens: Count = 0
    output_tokens: Count = 0
    voice_seconds: Count = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Settlement(Value):
    # Missing fields retain at least the reservation and observed cumulative lower bound.
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    voice_seconds: Count | None = None


class Limits(Value):
    turns: Count = 10000
    concurrent_runs: Count = 32
    tokens: Count = 100_000_000
    voice_seconds: Count = 36000


class Policy(Value):
    tenant: Limits = Field(default_factory=Limits)
    user: Limits = Field(default_factory=lambda: Limits(turns=2000, concurrent_runs=8))
    device: Limits = Field(default_factory=lambda: Limits(turns=2000, concurrent_runs=8))
    run_seconds: Annotated[int, Field(strict=True, ge=60, le=3600)] = 1800
    model_input_tokens: Positive = 262144


class UsageView(Value):
    period: str
    turns: Count
    voice_seconds: Count
    remaining_turns: Count
    quota_state: Literal["available", "limited", "exhausted"]


class QuotaExceeded(Exception):
    def __init__(self) -> None:
        super().__init__("rate_limited")

"""Shared resource visibility and display constraints, independent of authorization."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Version = Annotated[int, Field(strict=True, ge=1)]


class Visibility(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"


class DisplayPolicy(StrEnum):
    NEVER = "never"
    AUTHENTICATED_SESSION = "authenticated_session"
    SHARED_DISPLAY = "shared_display"


class Scope(BaseModel):
    """A complete resource scope. PATCH callers must merge with stored scope first."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"visibility": {"const": "private"}}},
                    "then": {
                        "properties": {
                            "display_policy": {"enum": ["never", "authenticated_session"]}
                        }
                    },
                }
            ]
        },
    )

    visibility: Visibility
    display_policy: DisplayPolicy

    @model_validator(mode="after")
    def validate_display_policy(self) -> Self:
        if (
            self.visibility == Visibility.PRIVATE
            and self.display_policy == DisplayPolicy.SHARED_DISPLAY
        ):
            raise ValueError("Private resources cannot use shared_display")
        return self

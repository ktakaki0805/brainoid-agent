"""Global account projection shared by HTTP and current-state policy."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from brainoid_contracts import FirebaseUid, IanaTimezone, Version


class UserProfile(BaseModel):
    """Public OpenAPI User projection; Firestore-only metadata never leaves this model."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    user_id: FirebaseUid
    display_name: Annotated[str, Field(strict=True, min_length=1, max_length=200)]
    timezone: IanaTimezone
    status: Literal["active", "deleting"]
    version: Version

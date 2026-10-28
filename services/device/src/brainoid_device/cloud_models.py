"""Strict device-owned journal and local DTOs; secrets never participate in repr."""

from typing import Annotated, Any, Literal

from brainoid_contracts import ResourceId, UtcTimestamp
from pydantic import BaseModel, ConfigDict, Field

Secret = Annotated[str, Field(strict=True, min_length=32, max_length=16384, pattern=r"^[!-~]+$")]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, strict=True)


class Credentials(Record):
    device_id: ResourceId
    tenant_id: ResourceId
    access_token: Secret = Field(repr=False)
    refresh_token: Annotated[str, Field(min_length=32, max_length=512, pattern=r"^[!-~]+$")] = (
        Field(repr=False)
    )
    expires_in: Literal[900]
    token_type: Literal["Bearer"]


class Pairing(Record):
    pairing_id: ResourceId
    user_code: Annotated[str, Field(pattern=r"^[A-Z0-9]{8}$")] = Field(repr=False)
    poll_secret: Annotated[str, Field(min_length=32, max_length=256, pattern=r"^[!-~]+$")] = Field(
        repr=False
    )
    expires_at: UtcTimestamp


class Pending(Record):
    kind: Literal["create", "exchange", "refresh", "conversation", "turn", "cancel"]
    key: ResourceId
    body: dict[str, Any] = Field(repr=False)
    body_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    path: str
    started: float
    attempts: Annotated[int, Field(ge=0, le=3)] = 0
    # Old journals with an attempted mutation are conservatively unresolved.
    uncertain: bool = True
    session: ResourceId | None = None
    result: dict[str, Any] | None = Field(default=None, repr=False)


class Cursor(Record):
    run_id: ResourceId
    session: ResourceId | None = None
    seq: Annotated[int, Field(ge=0)] = 0
    terminal: bool = False


class Journal(Record):
    version: Literal[1] = 1
    api: str
    installation_nonce: Secret = Field(repr=False)
    credentials: Credentials | None = Field(default=None, repr=False)
    pairing: Pairing | None = Field(default=None, repr=False)
    exchange_key: ResourceId | None = None
    pending_auth: Pending | None = Field(default=None, repr=False)
    pending_mutation: Pending | None = Field(default=None, repr=False)
    access_until: float = 0.0
    credential_version: Annotated[int, Field(ge=0)] = 0
    family_until: float = 0.0
    observed_at: float = 0.0
    stopped: bool = False
    cursors: Annotated[list[Cursor], Field(max_length=32)] = Field(default_factory=list)


class CloudError(Exception):
    def __init__(self, code: str = "cloud_unavailable", status: int = 503) -> None:
        self.code, self.status = code, status
        super().__init__(code)

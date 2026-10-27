"""Global push destinations. Tokens never enter notification receipts or public DTOs."""

from typing import Annotated, Literal, Self

from brainoid_contracts import FirebaseUid, ResourceId, UtcTimestamp, Version
from brainoid_contracts.access import fingerprint
from brainoid_contracts.transactions import Transaction
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_INSTALLATIONS = 20
ACCOUNT_GLOBALS = ("push_installations", "push_token_claims")
Token = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
AppVersion = Annotated[str, Field(strict=True, max_length=40)]


class PushCreate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    fcm_token: Token = Field(repr=False)
    platform: Literal["ios", "android"]
    app_version: AppVersion


class PushPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    fcm_token: Token = Field(repr=False)
    app_version: AppVersion | None = None

    @model_validator(mode="after")
    def no_explicit_null(self) -> Self:
        if "app_version" in self.model_fields_set and self.app_version is None:
            raise ValueError("app_version must be a string when supplied")
        return self


class PushView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: ResourceId
    platform: Literal["ios", "android"]
    version: Version
    created_at: UtcTimestamp


class Installation(PushView):
    user_id: FirebaseUid
    account_stamp: str
    generation: ResourceId
    status: Literal["active", "disabled", "removed"]
    fcm_token: Token | None = Field(default=None, repr=False)
    app_version: AppVersion


def installation_path(identity: ResourceId) -> str:
    return f"push_installations/{identity}"


def claim_path(token: str) -> str:
    return f"push_token_claims/{fingerprint(token)}"


def claim(record: Installation) -> dict[str, str]:
    return {"id": record.id, "user_id": record.user_id, "generation": record.generation}


def current_installation(tx: Transaction, record: Installation) -> bool:
    return (
        record.status == "active"
        and record.fcm_token is not None
        and tx.get(claim_path(record.fcm_token)) == claim(record)
    )


def view(record: Installation) -> PushView:
    return PushView.model_validate({key: getattr(record, key) for key in PushView.model_fields})

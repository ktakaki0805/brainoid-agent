"""Explicit, immutable API configuration; no dotenv or SDK default discovery."""

import os
from collections.abc import Mapping
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from brainoid_contracts import ResourceId
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.()-]+$")]


class ConfigurationError(RuntimeError):
    """Safe to print: contains setting names and rules, never supplied values."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    app_env: Literal["local-fake", "local-emulator", "staging", "production"]
    auth_provider: Literal["fake", "firebase"]
    model_provider: Literal["fake", "vertex"]
    gcp_project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
    gcp_region: Identifier
    firestore_database_id: Identifier
    stt_provider: Literal["disabled", "google-v2"] = "disabled"
    stt_location: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")] | None = None
    stt_model: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,127}$")] | None = None
    stt_recognizer: Annotated[str, Field(pattern=r"^(?:_|[a-z][a-z0-9-]{0,62})$")] | None = None
    tts_provider: Literal["disabled", "google-v1"] = "disabled"
    tts_location: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")] | None = None
    tts_voice: Annotated[str, Field(pattern=r"^ja-JP-[A-Za-z0-9-]{1,100}$")] | None = None
    model_id: Identifier
    firestore_emulator_host: str | None = None
    firebase_auth_emulator_host: str | None = None
    client_origins: tuple[str, ...] = ()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    readiness_timeout_seconds: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 2.0
    cursor_signing_key: Annotated[SecretStr, Field(min_length=32, max_length=512)] | None = None
    device_token_provider: Literal["disabled", "local-test", "pem"]
    device_token_keyring_file: (
        Annotated[str, Field(min_length=2, max_length=4096, pattern=r"^/")] | None
    ) = None
    device_credential_epoch: ResourceId | None = None
    authz_ledger_bucket: (
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")] | None
    ) = None
    authz_ledger_directory: (
        Annotated[str, Field(min_length=2, max_length=4096, pattern=r"^/")] | None
    ) = None

    @field_validator("client_origins", mode="before")
    @classmethod
    def parse_client_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",")) if value else ()
        return value

    @field_validator("client_origins")
    @classmethod
    def validate_client_origins(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 16 or len(set(values)) != len(values):
            raise ValueError("client origins must be unique, at most 16")
        for value in values:
            try:
                parsed = urlsplit(value)
                valid = (
                    parsed.hostname is not None
                    and parsed.scheme in ("https", "http", "capacitor")
                    and parsed.username is None
                    and parsed.password is None
                    and not parsed.path
                    and not parsed.query
                    and not parsed.fragment
                    and "*" not in value
                    and not any(c.isspace() for c in value)
                    and (parsed.port is None or 1 <= parsed.port <= 65535)
                    and (parsed.scheme != "capacitor" or value == "capacitor://localhost")
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("client origins must be explicit origins without URL components")
        return values

    @field_validator("firestore_emulator_host", "firebase_auth_emulator_host")
    @classmethod
    def validate_emulator_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit("//" + value)
            valid = (
                parsed.hostname is not None
                and parsed.port is not None
                and 1 <= parsed.port <= 65535
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and not any(character.isspace() for character in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "emulator host must be host:port without credentials or URL components"
            )
        return value

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        tts = (self.tts_location, self.tts_voice)
        if self.tts_provider == "google-v1":
            if self.app_env not in ("staging", "production") or not all(tts):
                raise ValueError("TTS requires cloud environment and explicit location and voice")
        elif any(value is not None for value in tts):
            raise ValueError("Disabled TTS cannot have provider settings")
        stt = (self.stt_location, self.stt_model, self.stt_recognizer)
        if self.stt_provider == "google-v2":
            if self.app_env not in ("staging", "production") or not all(stt):
                raise ValueError(
                    "STT requires cloud environment and explicit location, model, recognizer"
                )
        elif any(value is not None for value in stt):
            raise ValueError("Disabled STT cannot have provider settings")
        for origin in self.client_origins:
            parsed = urlsplit(origin)
            if parsed.scheme == "http" and not (
                self.app_env.startswith("local-") and parsed.hostname == "127.0.0.1"
            ):
                raise ValueError("client origins require HTTPS, native localhost or local loopback")
        if self.app_env == "local-fake":
            if (
                self.device_token_provider != "disabled"
                or self.device_token_keyring_file is not None
                or self.device_credential_epoch is not None
            ):
                raise ValueError("local-fake requires disabled device tokens")
        else:
            expected_provider = "local-test" if self.app_env == "local-emulator" else "pem"
            if (
                self.device_token_provider != expected_provider
                or self.device_token_keyring_file is None
                or self.device_credential_epoch is None
            ):
                raise ValueError(
                    "explicit device token provider, keyring and credential epoch required"
                )
        if self.app_env == "local-emulator":
            if self.authz_ledger_directory is None or self.authz_ledger_bucket is not None:
                raise ValueError("local-emulator requires only an explicit local ledger directory")
        elif self.app_env in ("staging", "production"):
            if self.authz_ledger_bucket is None or self.authz_ledger_directory is not None:
                raise ValueError("cloud requires only an explicit independent ledger bucket")
        elif self.authz_ledger_directory is not None or self.authz_ledger_bucket is not None:
            raise ValueError("local-fake cannot configure an authorization ledger")
        if self.auth_provider == "firebase" and self.cursor_signing_key is None:
            raise ValueError("firebase requires an explicit cursor signing key")
        hosts = (self.firestore_emulator_host, self.firebase_auth_emulator_host)
        if self.app_env == "local-fake":
            if self.auth_provider != "fake" or self.model_provider != "fake":
                raise ValueError("local-fake requires fake auth and model providers")
            if any(host is not None for host in hosts):
                raise ValueError("emulator hosts require local-emulator")
        elif self.app_env == "local-emulator":
            if self.auth_provider != "firebase" or self.model_provider != "fake":
                raise ValueError("local-emulator requires firebase auth and a fake model provider")
            if any(host is None for host in hosts):
                raise ValueError("local-emulator requires both emulator hosts")
        else:
            if self.auth_provider != "firebase" or self.model_provider != "vertex":
                raise ValueError("staging and production require firebase auth and vertex model")
            if any(host is not None for host in hosts):
                raise ValueError("emulator hosts are only allowed in local-emulator")
        if self.app_env.startswith("local-") and not self.gcp_project_id.startswith("demo-"):
            raise ValueError("local environments require a demo- project ID")
        return self


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read only documented variables, rejecting SDK project overrides that disagree."""
    environment = os.environ if environ is None else environ
    values = {
        name: environment[name.upper()]
        for name in Settings.model_fields
        if name.upper() in environment
    }
    try:
        settings = Settings.model_validate(values)
    except ValidationError as error:
        # Pydantic errors can carry the whole input and exception context. Do not log them.
        fields = sorted(
            {
                str(item["loc"][0]).upper() if item["loc"] else "environment rules"
                for item in error.errors()
            }
        )
        raise ConfigurationError("Invalid API configuration: " + ", ".join(fields)) from None
    for alias in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        if alias in environment and environment[alias] != settings.gcp_project_id:
            raise ConfigurationError(alias + " must match GCP_PROJECT_ID")
    return settings

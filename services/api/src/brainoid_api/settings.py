"""Explicit, immutable API configuration; no dotenv or SDK default discovery."""

import os
from collections.abc import Mapping
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    model_id: Identifier
    firestore_emulator_host: str | None = None
    firebase_auth_emulator_host: str | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    readiness_timeout_seconds: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 2.0

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

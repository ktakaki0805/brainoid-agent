"""Explicit local settings; diagnostics contain names and reasons, never supplied values."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """A safe operator-facing diagnostic."""


def directory(value: str, setting: str) -> Path:
    path = Path(value)
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path or not path.is_dir():
            raise ValueError
        # Resolve equality also excludes symlinks in any ancestor and '..'.
    except (OSError, ValueError, RuntimeError):
        raise ConfigurationError(
            f"{setting}: requires an existing absolute canonical directory"
        ) from None
    return path


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    static_dir: Path
    credentials_dir: Path

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "::1"}:
            raise ConfigurationError("DEVICE_HOST: requires 127.0.0.1 or ::1")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ConfigurationError("DEVICE_PORT: requires an unprivileged TCP port (1024..65535)")
        directory(str(self.static_dir), "DEVICE_STATIC_DIR")
        directory(str(self.credentials_dir), "DEVICE_CREDENTIALS_DIR")
        if self.static_dir.is_relative_to(
            self.credentials_dir
        ) or self.credentials_dir.is_relative_to(self.static_dir):
            raise ConfigurationError("DEVICE_STATIC_DIR: must be separate from credential storage")

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if self.host == "::1" else self.host
        return f"{host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.authority}"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    if os.geteuid() == 0:
        raise ConfigurationError("service user: must be a non-root account")
    names = ("DEVICE_HOST", "DEVICE_PORT", "DEVICE_STATIC_DIR", "DEVICE_CREDENTIALS_DIR")
    for name in names:
        if not source.get(name):
            raise ConfigurationError(f"{name}: required")
    if source["DEVICE_HOST"] not in {"127.0.0.1", "::1"}:
        raise ConfigurationError("DEVICE_HOST: requires 127.0.0.1 or ::1")
    port_value = source["DEVICE_PORT"]
    if (
        len(port_value) > 5
        or not port_value.isascii()
        or not port_value.isdecimal()
        or not 1024 <= int(port_value) <= 65535
    ):
        raise ConfigurationError("DEVICE_PORT: requires an unprivileged TCP port (1024..65535)")
    static_dir = directory(source["DEVICE_STATIC_DIR"], "DEVICE_STATIC_DIR")
    credentials_dir = directory(source["DEVICE_CREDENTIALS_DIR"], "DEVICE_CREDENTIALS_DIR")
    if static_dir.is_relative_to(credentials_dir) or credentials_dir.is_relative_to(static_dir):
        raise ConfigurationError("DEVICE_STATIC_DIR: must be separate from credential storage")
    return Settings(source["DEVICE_HOST"], int(port_value), static_dir, credentials_dir)

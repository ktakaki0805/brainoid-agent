"""Explicit local settings; diagnostics contain names and reasons, never supplied values."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
class CloudSettings:
    api_url: str
    local_test: bool = False

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.api_url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.scheme not in {"https", "http"}
                or any(c.isspace() or c in "\\%" for c in self.api_url)
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or (
                    parsed.scheme == "http"
                    and not (self.local_test and parsed.hostname in {"127.0.0.1", "::1"})
                )
                or (self.local_test and parsed.hostname not in {"127.0.0.1", "::1"})
            ):
                raise ValueError
        except ValueError:
            raise ConfigurationError("DEVICE_CLOUD_API: requires a trusted HTTPS origin") from None


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    static_dir: Path
    credentials_dir: Path
    state_dir: Path
    cloud: CloudSettings | None = None
    audio_backend: str = "disabled"

    def __post_init__(self) -> None:
        if self.audio_backend not in {"disabled", "alsa"}:
            raise ConfigurationError("DEVICE_AUDIO_BACKEND: requires disabled or alsa")
        if self.audio_backend != "disabled" and self.cloud is None:
            raise ConfigurationError("DEVICE_AUDIO_BACKEND: requires cloud configuration")
        if self.host not in {"127.0.0.1", "::1"}:
            raise ConfigurationError("DEVICE_HOST: requires 127.0.0.1 or ::1")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ConfigurationError("DEVICE_PORT: requires an unprivileged TCP port (1024..65535)")
        directory(str(self.static_dir), "DEVICE_STATIC_DIR")
        directory(str(self.credentials_dir), "DEVICE_CREDENTIALS_DIR")
        directory(str(self.state_dir), "DEVICE_STATE_DIR")
        paths = (self.static_dir, self.credentials_dir, self.state_dir)
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ConfigurationError(
                        "DEVICE_STATIC_DIR: storage directories must be separate"
                    )

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
    names = (
        "DEVICE_HOST",
        "DEVICE_PORT",
        "DEVICE_STATIC_DIR",
        "DEVICE_CREDENTIALS_DIR",
        "DEVICE_STATE_DIR",
    )
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
    state_dir = directory(source["DEVICE_STATE_DIR"], "DEVICE_STATE_DIR")
    cloud = None
    if source.get("DEVICE_CLOUD_API"):
        mode = source.get("DEVICE_CLOUD_MODE", "production")
        if mode not in {"production", "local-test"}:
            raise ConfigurationError("DEVICE_CLOUD_MODE: requires production or local-test")
        cloud = CloudSettings(source["DEVICE_CLOUD_API"], local_test=mode == "local-test")
    elif source.get("DEVICE_CLOUD_MODE"):
        raise ConfigurationError("DEVICE_CLOUD_API: required when cloud mode is set")
    return Settings(
        source["DEVICE_HOST"],
        int(port_value),
        static_dir,
        credentials_dir,
        state_dir,
        cloud,
        source.get("DEVICE_AUDIO_BACKEND", "disabled"),
    )

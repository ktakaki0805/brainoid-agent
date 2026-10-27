"""Private daemon-only files. No HTTP route exposes this store or its file names."""

import os
import re
import secrets
import stat
from pathlib import Path

from brainoid_device.config import ConfigurationError
from brainoid_device.filesystem import open_directory

MAX_CREDENTIAL_BYTES = 65536


def check_private_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size > MAX_CREDENTIAL_BYTES
    ):
        raise ConfigurationError(
            "DEVICE_CREDENTIALS_DIR: files require service ownership and mode 0600"
        )


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self._fd = -1
        try:
            self._fd = open_directory(path)
            info = os.fstat(self._fd)
            if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
                raise ConfigurationError(
                    "DEVICE_CREDENTIALS_DIR: requires service ownership and mode 0700"
                )
            for name in os.listdir(self._fd):
                check_private_file(os.stat(name, dir_fd=self._fd, follow_symlinks=False))
        except (OSError, ValueError):
            self.close()
            raise ConfigurationError(
                "DEVICE_CREDENTIALS_DIR: inaccessible or unsafe owner, mode or file type"
            ) from None

    @staticmethod
    def _name(name: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name) is None:
            raise ValueError("invalid credential name")
        return name

    def read(self, name: str) -> bytes:
        fd = os.open(self._name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        with os.fdopen(fd, "rb") as source:
            check_private_file(os.fstat(source.fileno()))
            value = source.read(MAX_CREDENTIAL_BYTES + 1)
        if len(value) > MAX_CREDENTIAL_BYTES:
            raise ValueError("credential exceeds size limit")
        return value

    def write(self, name: str, value: bytes) -> None:
        name = self._name(name)
        if len(value) > MAX_CREDENTIAL_BYTES:
            raise ValueError("credential exceeds size limit")
        temporary = ".pending-" + secrets.token_hex(16)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd
        )
        try:
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(value)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

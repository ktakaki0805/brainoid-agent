"""Private daemon-only files. No HTTP route exposes this store or its file names."""

import fcntl
import os
import re
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from brainoid_device.config import ConfigurationError
from brainoid_device.filesystem import open_directory

MAX_CREDENTIAL_BYTES = 65536
RESET_MARKER = ".reset-pending"


class CredentialsUnavailable(RuntimeError):
    """Fixed diagnostic: a reset fences every reader and writer."""


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
        self._lock = threading.RLock()
        self._blocked = False
        try:
            self._fd = open_directory(path)
            info = os.fstat(self._fd)
            if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
                raise ConfigurationError(
                    "DEVICE_CREDENTIALS_DIR: requires service ownership and mode 0700"
                )
            with self._exclusive():
                # A pending reset must be resumable even if a credential file was
                # left malformed. It will be unlinked, never opened or returned.
                if not self._pending():
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
        with self._exclusive():
            self._require_available()
            fd = os.open(
                self._name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd
            )
            with os.fdopen(fd, "rb") as source:
                check_private_file(os.fstat(source.fileno()))
                value = source.read(MAX_CREDENTIAL_BYTES + 1)
            if len(value) > MAX_CREDENTIAL_BYTES:
                raise ValueError("credential exceeds size limit")
            return value

    def write(self, name: str, value: bytes) -> None:
        with self._exclusive():
            self._require_available()
            self._write(name, value)

    def _write(self, name: str, value: bytes) -> None:
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

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _pending(self) -> bool:
        # lstat: any entry at the reserved name is a fence, even a damaged one.
        try:
            os.stat(RESET_MARKER, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _require_available(self) -> None:
        if self._blocked or self._pending():
            raise CredentialsUnavailable("credential_reset_pending")

    @property
    def reset_pending(self) -> bool:
        with self._exclusive():
            return self._blocked or self._pending()

    def begin_reset(self) -> None:
        """Persist the cross-store intent *before* deleting anything."""
        with self._exclusive():
            self._blocked = True
            fd = os.open(
                RESET_MARKER,
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=self._fd,
            )
            try:
                check_private_file(os.fstat(fd))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self._fd)

    def erase_for_reset(self) -> None:
        with self._exclusive():
            if not self._pending():
                raise CredentialsUnavailable("credential_reset_intent_required")
            for name in os.listdir(self._fd):
                if name != RESET_MARKER:
                    os.unlink(name, dir_fd=self._fd)
            os.fsync(self._fd)

    def finish_reset(self) -> None:
        """Caller must have durably cleared SQLite and completed private cleanup."""
        with self._exclusive():
            if set(os.listdir(self._fd)) != {RESET_MARKER}:
                raise CredentialsUnavailable("credential_reset_incomplete")
            os.unlink(RESET_MARKER, dir_fd=self._fd)
            os.fsync(self._fd)
            self._blocked = False

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

"""Durable create-only files for the explicitly selected local emulator only."""

import os
import tempfile
from pathlib import Path

from brainoid_policy.control_ledger import LedgerError


class LocalAuthzObjects:
    def __init__(self, directory: str) -> None:
        self._root = Path(directory)
        if not self._root.is_absolute():
            raise ValueError("Local ledger requires an absolute directory")

    def _path(self, name: str) -> Path:
        if not name.startswith(("authz/v1/", "deletion/v1/")) or any(
            part in ("", ".", "..") for part in name.split("/")
        ):
            raise LedgerError()
        return self._root / name

    def read(self, name: str) -> bytes | None:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            raise LedgerError() from None

    def create(self, name: str, data: bytes) -> bool:
        temporary: str | None = None
        try:
            path = self._path(name)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
                temporary = file.name
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, path)  # Atomic publication; never exposes a partial file.
            except FileExistsError:
                return False
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return True
        except OSError:
            raise LedgerError() from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def names(self, prefix: str) -> list[str]:
        try:
            directory = self._path(prefix.rstrip("/"))
            return sorted(
                str(path.relative_to(self._root))
                for path in directory.iterdir()
                if path.name.endswith(".json")
            )
        except FileNotFoundError:
            return []
        except OSError:
            raise LedgerError() from None

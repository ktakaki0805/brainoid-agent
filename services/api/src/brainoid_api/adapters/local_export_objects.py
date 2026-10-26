"""Explicit local encrypted temporary store; not a GCS adapter or default runtime.

The caller owns a dedicated directory and an explicit 256-bit encryption key.
Metadata and ZIP bytes are authenticated together. A persistent per-key revocation
marker and flock serialize put/revoke across threads and local processes.
"""

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from brainoid_contracts import ResourceId
from brainoid_memory.export_manifest import (
    MAX_BYTES,
    Artifact,
    Clock,
    ObjectRevoked,
    ObjectUnavailable,
)
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import TypeAdapter, ValidationError


class LocalExportObjects:
    def __init__(self, directory: Path, key: bytes, *, now: Clock) -> None:
        if len(key) != 32:
            raise ValueError("An explicit 256-bit export key is required")
        self.directory, self.cipher, self.now = directory, AESGCM(key), now
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            raise ValueError("Export directory must be private")

    @contextmanager
    def locked(self, key: ResourceId) -> Iterator[Path]:
        identity = TypeAdapter(ResourceId).validate_python(key)
        path = self.directory / identity
        fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield path
        finally:
            os.close(fd)

    def sync(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def put(self, key: ResourceId, content: bytes, artifact: Artifact) -> None:
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ObjectUnavailable()
        try:
            with self.locked(key) as path:
                if path.with_suffix(".revoked").exists() or self.now() >= artifact.expires_at:
                    raise ObjectRevoked()
                if path.with_suffix(".sealed").exists():
                    saved, meta = self._get(path, key)
                    if saved != content or meta != artifact:
                        raise ObjectUnavailable()
                    return
                metadata = artifact.model_dump_json().encode()
                plaintext = len(metadata).to_bytes(4, "big") + metadata + content
                nonce = os.urandom(12)
                sealed = nonce + self.cipher.encrypt(nonce, plaintext, key.encode())
                pending = path.with_suffix(".pending")
                fd = os.open(pending, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(sealed)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(pending, path.with_suffix(".sealed"))
                self.sync()
        except OSError:
            raise ObjectUnavailable() from None

    def _get(self, path: Path, key: ResourceId) -> tuple[bytes, Artifact]:
        if path.with_suffix(".revoked").exists():
            raise ObjectRevoked()
        with path.with_suffix(".sealed").open("rb") as source:
            sealed = source.read(MAX_BYTES + 4096)
            if source.read(1):
                raise ObjectUnavailable()
        plaintext = self.cipher.decrypt(sealed[:12], sealed[12:], key.encode())
        length = int.from_bytes(plaintext[:4], "big")
        artifact = Artifact.model_validate_json(plaintext[4 : 4 + length])
        content = plaintext[4 + length :]
        if self.now() >= artifact.expires_at:
            raise ObjectRevoked()
        if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise ObjectUnavailable()
        return content, artifact

    def get(self, key: ResourceId) -> tuple[bytes, Artifact]:
        try:
            with self.locked(key) as path:
                return self._get(path, key)
        except (OSError, InvalidTag, ValueError, ValidationError):
            raise ObjectUnavailable() from None

    def revoke(self, key: ResourceId) -> None:
        try:
            with self.locked(key) as path:
                fd = os.open(path.with_suffix(".revoked"), os.O_CREAT | os.O_WRONLY, 0o600)
                os.fsync(fd)
                os.close(fd)
                self.sync()
                for suffix in (".sealed", ".pending"):
                    path.with_suffix(suffix).unlink(missing_ok=True)
                self.sync()
        except OSError:
            raise ObjectUnavailable() from None

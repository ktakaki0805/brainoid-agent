"""Bounded release format and durable, private updater files (Linux only)."""

import hashlib
import json
import os
import re
import stat
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from brainoid_device.filesystem import open_directory
from brainoid_device.migrations import SCHEMA_VERSION

MAX_ARCHIVE = 96 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 4096
RUNTIME_ABI = "brainoid-device-python313-v1"


class UpdateError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("device_update_recovery_required")


def read_file(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
            or info.st_size > limit
        ):
            raise UpdateError()
        data = source.read(limit + 1)
        if len(data) > limit:
            raise UpdateError()
        return data


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def atomic_json(path: Path, data: object) -> None:
    temporary = path.with_name(path.name + ".pending")
    # An interrupted earlier write is never read as committed state.
    temporary.unlink(missing_ok=True)
    write_new(temporary, json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
    os.replace(temporary, path)
    sync_directory(path.parent)


def private_directory(path: Path) -> int:
    fd = open_directory(path)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise UpdateError()
    return fd


def check_tree(path: Path, *, metadata: bool = False) -> None:
    """Reject pre-existing links/devices before any library or cleanup touches them."""
    fd = private_directory(path)
    os.close(fd)
    count = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            count += 1
            item = Path(directory) / name
            relative = item.relative_to(path).as_posix()
            info = item.lstat()
            if metadata and relative == "root.json" and stat.S_ISLNK(info.st_mode):
                if re.fullmatch(r"root_history/[1-9][0-9]*\.root\.json", os.readlink(item)):
                    continue
            if (
                count > 20000
                or len(item.relative_to(path).parts) > 18
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
            ):
                raise UpdateError()


def sync_tree(path: Path, *, metadata: bool = False) -> None:
    check_tree(path, metadata=metadata)
    for directory, _, files in os.walk(path, topdown=False):
        for name in files:
            item = Path(directory) / name
            if item.is_symlink():  # Only TUF's validated root alias is allowed.
                continue
            fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        sync_directory(Path(directory))


@dataclass(frozen=True)
class Manifest:
    format: int
    version: int
    os: str
    architecture: str
    protocol: int
    sqlite_schema: int
    runtime_abi: str

    @classmethod
    def parse(cls, data: bytes, system: str, machine: str) -> "Manifest":
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise UpdateError()
        manifest = cls(**value)
        if (
            any(
                type(value[key]) is not int
                for key in ("format", "version", "protocol", "sqlite_schema")
            )
            or manifest.format != 1
            or not 1 <= manifest.version < 2**53
            or system != "linux"
            or machine not in {"x86_64", "aarch64"}
            or manifest.os != system
            or manifest.architecture != machine
            or manifest.protocol != 1
            or manifest.sqlite_schema != SCHEMA_VERSION
            or manifest.runtime_abi != RUNTIME_ABI
        ):
            raise UpdateError()
        return manifest


@dataclass(frozen=True)
class Release:
    version: int
    sha256: str

    @property
    def name(self) -> str:
        return f"{self.version}-{self.sha256}"

    @classmethod
    def parse(cls, value: Any) -> "Release | None":
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"version", "sha256"}:
            raise UpdateError()
        result = cls(**value)
        if (
            type(result.version) is not int
            or not 1 <= result.version < 2**53
            or not isinstance(result.sha256, str)
            or re.fullmatch("[0-9a-f]{64}", result.sha256) is None
        ):
            raise UpdateError()
        return result

    def json(self) -> dict[str, Any]:
        return asdict(self)


def archive_files(archive: Path) -> dict[str, bytes]:
    """Never call extractall: only regular, explicitly named lib/UI files exist."""
    files: dict[str, bytes] = {}
    total = 0
    # Parse only fixed USTAR headers: tarfile's extension parser would read
    # PAX/GNU long-name payloads before product expansion limits could run.
    data = read_file(archive, MAX_ARCHIVE)
    offset = 0
    index = 0
    terminated = False
    while offset + 512 <= len(data):
        header = data[offset : offset + 512]
        offset += 512
        if header == bytes(512):
            if len(data) - offset < 512 or any(data[offset:]):
                raise UpdateError()
            terminated = True
            break
        if header[257:263] != b"ustar\x00" or header[156:157] not in {b"0", b"\x00"}:
            raise UpdateError()
        item = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        path = PurePosixPath(item.name)
        parts = path.parts
        if (
            index >= MAX_ENTRIES
            or not item.isreg()
            or item.pax_headers
            or not parts
            or len(parts) > 16
            or path.as_posix() != item.name
            or any(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", p) is None for p in parts)
            or any(p in {".", ".."} for p in parts)
            or item.name in files
            or item.size > MAX_FILE
            or item.size < 0
        ):
            raise UpdateError()
        if item.name != "release.json" and not (
            len(parts) >= 2
            and (
                (parts[0] == "lib" and path.suffix == ".py")
                or item.name
                in {
                    "lib/brainoid_device/device_protocol.schema.json",
                    "lib/brainoid_device/run_events.schema.json",
                }
                or (
                    parts[0] == "ui"
                    and path.suffix.lower()
                    in {
                        ".html",
                        ".js",
                        ".css",
                        ".svg",
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".webp",
                        ".avif",
                        ".ico",
                        ".woff",
                        ".woff2",
                    }
                )
            )
        ):
            raise UpdateError()
        total += item.size
        if total > MAX_EXPANDED:
            raise UpdateError()
        files[item.name] = data[offset : offset + item.size]
        if len(files[item.name]) != item.size:
            raise UpdateError()
        offset += ((item.size + 511) // 512) * 512
        index += 1
    if not terminated:
        raise UpdateError()
    if not {
        "release.json",
        "lib/brainoid_device/__init__.py",
        "lib/brainoid_device/__main__.py",
        "ui/index.html",
    }.issubset(files):
        raise UpdateError()
    return files


def release_files(archive: Path, release: Release, system: str, machine: str) -> dict[str, bytes]:
    if hashlib.sha256(read_file(archive, MAX_ARCHIVE)).hexdigest() != release.sha256:
        raise UpdateError()
    files = archive_files(archive)
    manifest = Manifest.parse(files["release.json"], system, machine)
    if manifest.version != release.version:
        raise UpdateError()
    return files


def extract(archive: Path, tree: Path, release: Release, system: str, machine: str) -> None:
    files = release_files(archive, release, system, machine)
    tree.mkdir(mode=0o700)
    for name, data in files.items():
        target = tree / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_new(target, data)
    sync_tree(tree)


def verify_tree(directory: Path, release: Release, system: str, machine: str) -> Path:
    check_tree(directory)
    files = release_files(directory / "artifact.tar", release, system, machine)
    tree = directory / "tree"
    expected = set(files)
    actual = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    if actual != expected:
        raise UpdateError()
    for name, data in files.items():
        if read_file(tree / name, MAX_FILE) != data:
            raise UpdateError()
    return tree

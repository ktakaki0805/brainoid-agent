"""Bounded, immutable UI snapshot; requests never open paths on the filesystem."""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from brainoid_device.config import ConfigurationError
from brainoid_device.filesystem import open_directory

TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_FILES = 4096


@dataclass(frozen=True)
class Asset:
    content: bytes
    media_type: str


def load_assets(path: Path) -> dict[str, Asset]:
    assets: dict[str, Asset] = {}
    total = 0
    entries = 0

    def visit(fd: int, prefix: str, depth: int) -> None:
        nonlocal total, entries
        info = os.fstat(fd)
        if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022 or depth > 16:
            raise ValueError
        for name in os.listdir(fd):
            entries += 1
            if entries > MAX_FILES or name.startswith(".") or any(c in name for c in "%\\\x00"):
                raise ValueError
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child_fd, prefix + name + "/", depth + 1)
                finally:
                    os.close(child_fd)
            else:
                media_type = TYPES.get(Path(name).suffix.lower())
                if not media_type:
                    raise ValueError
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(file_fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_uid not in {0, os.geteuid()}
                        or info.st_mode & 0o022
                        or info.st_size > MAX_FILE
                    ):
                        raise ValueError
                    content = source.read(MAX_FILE + 1)
                total += len(content)
                if len(content) > MAX_FILE or total > MAX_TOTAL:
                    raise ValueError
                assets[prefix + name] = Asset(content, media_type)

    try:
        root_fd = open_directory(path)
        try:
            visit(root_fd, "/", 0)
        finally:
            os.close(root_fd)
        if "/index.html" not in assets:
            raise ValueError
    except (OSError, ValueError):
        raise ConfigurationError(
            "DEVICE_STATIC_DIR: requires index.html and a safe, bounded built asset tree"
        ) from None
    assets["/"] = assets["/index.html"]
    return assets

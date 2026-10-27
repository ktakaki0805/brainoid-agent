"""Open trusted directory chains without following symlinks, including ancestors."""

import os
import stat
from pathlib import Path


def open_directory(path: Path) -> int:
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError("directory must be absolute and canonical")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            info = os.fstat(fd)
            # Root-owned sticky /tmp is safe: other users cannot replace our entry.
            sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.geteuid()} or (info.st_mode & 0o022 and not sticky_root):
                raise ValueError("unsafe directory ancestor")
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

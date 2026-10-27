"""Crash-only synthetic supervisor fixture: no host service operations."""

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from brainoid_device.update_runtime import OwnedDaemon, check_storage
from brainoid_device.updater import ApplicationUpdater
from tuf.api.exceptions import DownloadHTTPError
from tuf.ngclient.fetcher import FetcherInterface


class DiskRepository(FetcherInterface):
    def __init__(self, root: Path) -> None:
        self.root = root

    def _fetch(self, url: str) -> Iterator[bytes]:
        path = self.root / urlsplit(url).path.lstrip("/")
        if not path.is_file():
            raise DownloadHTTPError("synthetic missing file", 404)
        yield path.read_bytes()


def main() -> None:
    path, phase, port = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    daemon = OwnedDaemon(path / "state", path / "private", "127.0.0.1", port)
    updater = ApplicationUpdater(
        path / "updates",
        (path / "trust.json").read_bytes(),
        "https://fixture.invalid/",
        daemon,
        lambda: check_storage(path / "state", path / "private"),
        fetcher=DiskRepository(path / "repository"),
    )
    updater.resume()

    def crash(point: str) -> None:
        if point == phase:
            child = daemon.process
            (path / "owned.json").write_text(json.dumps({"pid": child.pid if child else None}))
            os._exit(77)

    updater.checkpoint = crash
    updater.activate(updater.prepare("device-2.tar"))
    raise AssertionError("crash boundary not visited")


if __name__ == "__main__":
    main()

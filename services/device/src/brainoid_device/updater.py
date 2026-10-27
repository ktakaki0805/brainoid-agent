"""TUF discovery, versioned application activation and crash recovery.

Explicit local supervisor; unrelated services and Agent tools cannot trigger it.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, Protocol
from urllib.parse import urlsplit

from tuf.ngclient.config import UpdaterConfig
from tuf.ngclient.fetcher import FetcherInterface
from tuf.ngclient.updater import Updater
from tuf.ngclient.urllib3_fetcher import Urllib3Fetcher

from brainoid_device.config import directory
from brainoid_device.update_files import (
    MAX_ARCHIVE,
    Manifest,
    Release,
    UpdateError,
    archive_files,
    atomic_json,
    check_tree,
    extract,
    private_directory,
    read_file,
    sync_directory,
    sync_tree,
    verify_tree,
)
from brainoid_device.update_runtime import OwnedDaemon, check_storage


class Daemon(Protocol):
    def start(self, tree: Path) -> None: ...
    def healthy(self) -> bool: ...
    def stop(self) -> None: ...
    def stopped(self) -> bool: ...


def noop(phase: str) -> None:
    pass


@contextmanager
def download_deadline() -> Iterator[None]:
    def expired(signum: int, frame: FrameType | None) -> None:
        raise UpdateError()

    previous = signal.signal(signal.SIGALRM, expired)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 120)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *old_timer)
        signal.signal(signal.SIGALRM, previous)


class ApplicationUpdater:
    def __init__(
        self,
        root: Path,
        trust: bytes,
        repository: str,
        daemon: Daemon,
        storage_check: Callable[[], None],
        *,
        fetcher: FetcherInterface | None = None,
        checkpoint: Callable[[str], None] = noop,
    ) -> None:
        self.fd = -1
        self.daemon = daemon
        self.storage_check = storage_check
        self.checkpoint = checkpoint
        self.system = platform.system().lower()
        self.machine = platform.machine()
        self.root = directory(str(root), "DEVICE_UPDATE_DIR")
        self.trust = trust
        self.repository = repository.rstrip("/") + "/"
        self.fetcher = fetcher
        self.state: dict[str, Any] = {}
        try:
            self.fd = private_directory(self.root)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Never reuse an untrusted existing cache/tree or walk through links.
            for name in os.listdir(self.root):
                item = self.root / name
                if name in {"current", "current.pending"}:
                    if (
                        not item.is_symlink()
                        or re.fullmatch(
                            r"releases/[1-9][0-9]*-[0-9a-f]{64}/tree", os.readlink(item)
                        )
                        is None
                    ):
                        raise UpdateError()
                elif item.is_dir() and not item.is_symlink():
                    check_tree(item, metadata=name == "metadata")
                else:
                    read_file(item, 65536)
            (self.root / "current.pending").unlink(missing_ok=True)
            for name in ("releases", "metadata"):
                (self.root / name).mkdir(mode=0o700, exist_ok=True)
            trust_sha = hashlib.sha256(trust).hexdigest()
            journal = self.root / "state.json"
            if journal.exists():
                self.state = json.loads(read_file(journal, 65536))
                if (
                    set(self.state)
                    != {"format", "trust_sha256", "floor", "good", "pending", "phase"}
                    or self.state["format"] != 1
                    or self.state["trust_sha256"] != trust_sha
                    or type(self.state["floor"]) is not int
                    or not 0 <= self.state["floor"] < 2**53
                    or self.state["phase"] not in {"stable", "pending", "blocked"}
                ):
                    raise UpdateError()
                good = Release.parse(self.state["good"])
                pending = Release.parse(self.state["pending"])
                if any(r and r.version > self.state["floor"] for r in (good, pending)):
                    raise UpdateError()
                if self.state["phase"] == "stable" and pending is not None:
                    raise UpdateError()
            else:
                # Loss of the journal is not a new install when releases exist.
                if list((self.root / "releases").iterdir()) or (self.root / "current").is_symlink():
                    raise UpdateError()
                self.state = dict(
                    format=1,
                    trust_sha256=trust_sha,
                    floor=0,
                    good=None,
                    pending=None,
                    phase="stable",
                )
                self.save()
            # Leftover staging is never an application candidate.
            for item in self.root.glob("stage-*"):
                check_tree(item)
                shutil.rmtree(item)
            sync_directory(self.root)
        except BaseException:
            self.close()
            raise

    def save(self) -> None:
        atomic_json(self.root / "state.json", self.state)

    def pointer(self, release: Release) -> None:
        pending = self.root / "current.pending"
        pending.unlink(missing_ok=True)
        os.symlink(f"releases/{release.name}/tree", pending)
        self.checkpoint("pointer_staged")
        os.replace(pending, self.root / "current")
        sync_directory(self.root)
        self.checkpoint("pointer")

    def launch(self, release: Release) -> None:
        tree = verify_tree(
            self.root / "releases" / release.name, release, self.system, self.machine
        )
        self.storage_check()
        self.daemon.start(tree)
        self.checkpoint("launch")
        if not self.daemon.healthy():
            raise UpdateError()
        self.checkpoint("health")

    def block(self) -> None:
        self.state["phase"] = "blocked"
        self.save()
        (self.root / "current").unlink(missing_ok=True)
        sync_directory(self.root)

    def rollback(self) -> None:
        try:
            # Reassert the prior good identity if final commit replace succeeded
            # but its fsync failed. Only then change any runnable reference.
            self.save()
            # A failed new child can already have exited nonzero. stop() still
            # reaps/kills its group; only then can the old application be started.
            try:
                self.daemon.stop()
            except UpdateError:
                if not self.daemon.stopped():
                    raise
            good = Release.parse(self.state["good"])
            if good is None:
                raise UpdateError()
            self.storage_check()
            verify_tree(self.root / "releases" / good.name, good, self.system, self.machine)
            self.pointer(good)
            self.launch(good)
            restored = {**self.state, "phase": "stable", "pending": None}
            atomic_json(self.root / "state.json", restored)
            self.state = restored
            self.checkpoint("rollback")
        except Exception:
            try:
                self.daemon.stop()
            finally:
                self.block()
            raise UpdateError() from None

    def resume(self) -> None:
        if self.state["phase"] == "blocked":
            raise UpdateError()
        if self.state["phase"] == "pending":
            self.rollback()
            return
        good = Release.parse(self.state["good"])
        if good is not None:
            try:
                self.pointer(good)
                self.launch(good)
            except Exception:
                try:
                    self.daemon.stop()
                finally:
                    self.block()
                raise UpdateError() from None

    def prepare(self, target: str) -> Release:
        # Supported on the supervisor main thread; a slow peer cannot keep a
        # socket alive forever by dripping bytes inside each read timeout.
        with download_deadline():
            return self._prepare(target)

    def _prepare(self, target: str) -> Release:
        if (
            self.state["phase"] != "stable"
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.tar", target) is None
        ):
            raise UpdateError()
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.root))
        metadata = self.root / "metadata"
        check_tree(metadata, metadata=True)
        fetcher = self.fetcher or Urllib3Fetcher(socket_timeout=3)
        try:
            client = Updater(
                str(metadata),
                self.repository + "metadata/",
                target_base_url=self.repository + "targets/",
                bootstrap=self.trust,
                fetcher=fetcher,
                config=UpdaterConfig(
                    max_root_rotations=16,
                    max_delegations=8,
                    root_max_length=65536,
                    timestamp_max_length=16384,
                    snapshot_max_length=262144,
                    targets_max_length=1048576,
                ),
            )
            try:
                client.refresh()
                info = client.get_targetinfo(target)
            finally:
                # The library atomically replaces metadata, but does not fsync it.
                # Persist all verified intermediate state even on refresh failure.
                sync_tree(metadata, metadata=True)
            if info is None or not 0 < info.length <= MAX_ARCHIVE or "sha256" not in info.hashes:
                raise UpdateError()
            archive = stage / "artifact.tar"
            client.download_target(info, str(archive))
            os.chmod(archive, 0o600)
            self.checkpoint("download")
            manifest = Manifest.parse(
                archive_files(archive)["release.json"], self.system, self.machine
            )
            release = Release(manifest.version, info.hashes["sha256"])
            if release.version <= self.state["floor"]:
                raise UpdateError()
            extract(archive, stage / "tree", release, self.system, self.machine)
            self.checkpoint("extract")
            sync_tree(stage)
            destination = self.root / "releases" / release.name
            if destination.exists():
                verify_tree(destination, release, self.system, self.machine)
            else:
                os.rename(stage, destination)
                sync_directory(destination.parent)
                sync_directory(self.root)
            self.checkpoint("release")
            return release
        finally:
            if stage.exists():
                shutil.rmtree(stage)
                sync_directory(self.root)

    def activate(self, release: Release) -> bool:
        if self.state["phase"] != "stable" or release.version <= self.state["floor"]:
            raise UpdateError()
        verify_tree(self.root / "releases" / release.name, release, self.system, self.machine)
        self.state.update(floor=release.version, pending=release.json(), phase="pending")
        self.save()
        self.checkpoint("pending")
        try:
            # Daemon graceful shutdown clears sessions and joins all runtime audio
            # tasks/children before any runnable reference changes.
            self.daemon.stop()
            self.checkpoint("quiesced")
            self.storage_check()
            self.pointer(release)
            self.launch(release)
            committed = {**self.state, "good": release.json(), "pending": None, "phase": "stable"}
            atomic_json(self.root / "state.json", committed)
            self.state = committed
            self.checkpoint("committed")
            return True
        except Exception:
            self.rollback()
            return False

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class Stopped(BaseException):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(prog="brainoid-update")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--trust-root", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--target")
    parser.add_argument("--test-repository", action="store_true")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    updater: ApplicationUpdater | None = None
    daemon = OwnedDaemon(args.state, args.credentials, args.host, args.port)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def stop(signum: int, frame: FrameType | None) -> None:
        raise Stopped()

    result = 1
    try:
        if os.geteuid() == 0 or sys.platform != "linux":
            raise UpdateError()
        for sig in previous:
            signal.signal(sig, stop)
        roots = [
            directory(str(p), "updater directory")
            for p in (args.root, args.state, args.credentials)
        ]
        for index, root in enumerate(roots):
            for other in roots[index + 1 :]:
                if root.is_relative_to(other) or other.is_relative_to(root):
                    raise UpdateError()
        url = urlsplit(args.repository)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise UpdateError()
        if url.scheme != "https" and not (
            args.test_repository and url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}
        ):
            raise UpdateError()
        directory(str(args.trust_root.parent), "trust root parent")
        trust = read_file(args.trust_root, 65536)
        # Test trust is always explicit and cannot be accidentally used in normal mode.
        synthetic = json.loads(trust).get("signed", {}).get("brainoid_test_only", False)
        if synthetic is not False and not args.test_repository:
            raise UpdateError()
        updater = ApplicationUpdater(
            args.root,
            trust,
            args.repository,
            daemon,
            lambda: check_storage(args.state, args.credentials),
        )
        updater.resume()
        if args.target:
            updater.activate(updater.prepare(args.target))
        if daemon.process is None:
            raise UpdateError()
        print("device_update_serving", flush=True)
        while daemon.exited() is None:
            time.sleep(0.1)
        result = 0 if daemon.exited() == 0 else 1
    except Stopped:
        result = 0
    except Exception:
        print("device_update_recovery_required", file=sys.stderr)
        result = 1
    finally:
        # Repeated termination cannot interrupt the owned child's bounded cleanup.
        for sig in previous:
            signal.signal(sig, signal.SIG_IGN)
        try:
            daemon.stop()
        except Exception:
            result = 1
            print("device_update_shutdown_failed", file=sys.stderr)
        finally:
            if updater is not None:
                updater.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

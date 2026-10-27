"""Only the updater's own daemon session may be stopped or health checked."""

import ctypes
import http.client
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

from brainoid_device.config import Settings
from brainoid_device.ledger import DB_NAME, DeviceStorage
from brainoid_device.migrations import APPLICATION_ID, SCHEMA_VERSION
from brainoid_device.storage import CredentialStore
from brainoid_device.update_files import UpdateError, private_directory, read_file

GUARDIAN_NAME = "brainoid-guard"


def is_guardian(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() == GUARDIAN_NAME
    except (FileNotFoundError, ProcessLookupError):
        return False


# Fixed trusted launcher; the only inserted source directory is a verified release.
# Linux parent-death signal closes sessions/resources even if supervisor is killed.
LAUNCH = """
import ctypes, os, signal, sys
parent = int(sys.argv[1])
if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    raise SystemExit(1)
if os.getppid() != parent:
    raise SystemExit(1)
signal.pthread_sigmask(signal.SIG_SETMASK, [])
sys.path.insert(0, sys.argv[2])
from brainoid_device.__main__ import main
sys.argv = ['brainoid-device', 'serve']
raise SystemExit(main())
"""


def check_storage(state: Path, credentials: Path) -> None:
    """No DB migration or snapshot restore is allowed in this updater ABI."""
    # Bound header read rather than reading a potentially large command ledger.
    directory_fd = private_directory(state)
    try:
        fd = os.open(DB_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise UpdateError()
            header = os.read(fd, 100)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)
    if (
        header[:16] != b"SQLite format 3\x00"
        or int.from_bytes(header[60:64], "big") != SCHEMA_VERSION
        or int.from_bytes(header[68:72], "big") != APPLICATION_ID
    ):
        raise UpdateError()
    store = CredentialStore(credentials)
    try:
        if store.reset_pending:
            raise UpdateError()
        storage = DeviceStorage(state, store, cleanup_private=lambda: None)
        try:
            if storage.snapshot().status not in {"ready", "unregistered"}:
                raise UpdateError()
        finally:
            storage.close()
    finally:
        store.close()


class HealthExpired(Exception):
    """Private control flow: interrupts a synchronous HTTP read at the deadline."""


class OwnedDaemon:
    """Synchronous supervisor: no detached async launch, shell or borrowed PID."""

    def __init__(self, state: Path, credentials: Path, host: str, port: int) -> None:
        self.state = state
        self.credentials = credentials
        self.host = host
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.health_seconds = 8.0
        self.stop_seconds = 14.0
        self.kill_seconds = 2.0
        self._nonce = ""
        self._tree: Path | None = None

    def start(self, tree: Path) -> None:
        if self.process is not None:
            raise UpdateError()
        settings = Settings(self.host, self.port, tree / "ui", self.credentials, self.state)
        check_storage(self.state, self.credentials)
        # Root/source paths were checked before launch. Do not forward Python
        # injection variables, proxy URLs, or cloud credentials from the shell.
        env = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "DEVICE_HOST": settings.host,
            "DEVICE_PORT": str(settings.port),
            "DEVICE_STATIC_DIR": str(settings.static_dir),
            "DEVICE_STATE_DIR": str(settings.state_dir),
            "DEVICE_CREDENTIALS_DIR": str(settings.credentials_dir),
            "DEVICE_ACTIVATION_NONCE": secrets.token_hex(32),
        }
        self._nonce = env["DEVICE_ACTIVATION_NONCE"]
        self._tree = tree
        # Adopt only orphaned descendants; stop() reaps members of this owned
        # session, never arbitrary children of the hosting process.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise UpdateError()
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", LAUNCH, str(os.getpid()), str(tree / "lib")],
                cwd=tree,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            # Any queued termination is delivered only after ownership is set.
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def healthy(self) -> bool:
        # All supervisor work runs on the main thread. A single timer interrupts
        # connect/getresponse/read even when a peer drips bytes under its socket
        # inactivity timeout. No worker reader can survive this stack unwind.
        started = time.monotonic()
        deadline = started + self.health_seconds
        if self._tree is None or deadline <= started:
            return False

        def expired(signum: int, frame: FrameType | None) -> None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                signal.setitimer(signal.ITIMER_REAL, remaining)
            else:
                raise HealthExpired()

        previous = signal.signal(signal.SIGALRM, expired)
        old_timer = signal.setitimer(signal.ITIMER_REAL, self.health_seconds)
        try:
            return self._healthy(deadline) and time.monotonic() < deadline
        except HealthExpired:
            return False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if old_timer[0]:
                remaining = max(0.000001, old_timer[0] - (time.monotonic() - started))
                signal.setitimer(signal.ITIMER_REAL, remaining, old_timer[1])

    def _healthy(self, deadline: float) -> bool:
        tree = self._tree
        assert tree is not None
        while time.monotonic() < deadline:
            if self.process is None or self.exited() is not None:
                return False
            connection = http.client.HTTPConnection(self.host, self.port, timeout=0.3)
            try:
                connection.request("GET", "/healthz")
                with connection.getresponse() as response:
                    healthy = (
                        response.status == 200
                        and response.getheader("X-Brainoid-Activation") == self._nonce
                        and response.read(128) == b'{"status":"ok"}'
                    )
                connection.close()
                if healthy:
                    connection = http.client.HTTPConnection(self.host, self.port, timeout=0.3)
                    connection.request("GET", "/")
                    with connection.getresponse() as response:
                        # The local UI must also come from this child, not another listener.
                        healthy = response.status == 200 and response.read(
                            8 * 1024 * 1024 + 1
                        ) == read_file(tree / "ui/index.html", 8 * 1024 * 1024)
                    return healthy and self.exited() is None and time.monotonic() < deadline
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        return False

    def exited(self) -> int | None:
        child = self.process
        if child is None:
            return 0
        if child.returncode is not None:
            return child.returncode
        status = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status

    @staticmethod
    def identity(pid: int) -> tuple[int, str] | None:
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            return int(fields[3]), fields[19]  # session ID and kernel start ticks
        except (FileNotFoundError, ProcessLookupError):
            return None

    @classmethod
    def session_members(cls, leader: int) -> dict[int, str]:
        members = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal() or int(entry.name) == leader:
                continue
            pid = int(entry.name)
            identity = cls.identity(pid)
            # A descendant may use setpgrp() without leaving the owned session.
            if identity is not None and identity[0] == leader:
                members[pid] = identity[1]
        return members

    @classmethod
    def signal_member(cls, pid: int, birth: str, session: int, sig: signal.Signals) -> None:
        """Advance one nonblocking signal step; force callers rescan to their deadline."""
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            # Check after acquiring the stable handle. Reuse between enumeration
            # and pidfd_open must never turn a stale PID into an unrelated signal.
            if cls.identity(pid) == (session, birth):
                try:
                    if sig == signal.SIGKILL:
                        if is_guardian(pid):
                            # This trusted role never changes back. It is safe to
                            # request cleanup, including after our earlier STOP.
                            signal.pidfd_send_signal(fd, signal.SIGUSR1)
                            signal.pidfd_send_signal(fd, signal.SIGCONT)
                            return
                        # A negative role read is unstable: Python may enter the
                        # guardian and create a separate command session next.
                        # Stop it first; only a later observed stopped state can
                        # make the negative classification safe for SIGKILL.
                        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                        if fields[0] not in {"T", "t", "Z", "X"}:
                            signal.pidfd_send_signal(fd, signal.SIGSTOP)
                            return
                        if is_guardian(pid):
                            signal.pidfd_send_signal(fd, signal.SIGUSR1)
                            signal.pidfd_send_signal(fd, signal.SIGCONT)
                            return
                    signal.pidfd_send_signal(fd, sig)
                except (FileNotFoundError, ProcessLookupError):
                    pass
        finally:
            os.close(fd)

    @classmethod
    def signal_session(cls, leader: int, sig: signal.Signals) -> None:
        members = cls.session_members(leader)
        identity = cls.identity(leader)
        if identity is None or identity[0] != leader:
            raise UpdateError()
        members[leader] = identity[1]
        for pid, birth in members.items():
            cls.signal_member(pid, birth, leader, sig)

    @classmethod
    def reap_members(cls, leader: int) -> None:
        for pid, birth in cls.session_members(leader).items():
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                if cls.identity(pid) == (leader, birth):
                    try:
                        if is_guardian(pid):
                            status = os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                            if status is not None and (
                                status.si_code != os.CLD_EXITED or status.si_status != 0
                            ):
                                raise UpdateError()  # unknown command-tree state: retain ownership
                        os.waitid(os.P_PIDFD, fd, os.WEXITED | os.WNOHANG)
                    except (ChildProcessError, ProcessLookupError):
                        pass  # Not yet reparented from the dying owned daemon.
            finally:
                os.close(fd)

    def stopped(self) -> bool:
        return self.process is None

    def stop(self) -> None:
        child = self.process
        if child is None:
            return
        # A caller must not wait()/poll() our child. If already reaped, its PID
        # can be reused: never signal that numeric process group again.
        if child.returncode is not None:
            self.process = None
            if child.returncode != 0:
                raise UpdateError()
            return
        graceful = True
        self.signal_session(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + self.stop_seconds
        while time.monotonic() < deadline:
            self.reap_members(child.pid)
            code = self.exited()
            members = self.session_members(child.pid)
            if code is not None and (
                not members or (code != 0 and not any(is_guardian(pid) for pid in members))
            ):
                break
            time.sleep(0.02)
        if self.exited() is None or self.session_members(child.pid):
            graceful = False
            # The unreaped leader reserves the session identity. pidfds bind
            # each signal even when descendants occupy other process groups.
            self.signal_session(child.pid, signal.SIGKILL)
        deadline = time.monotonic() + self.kill_seconds
        while time.monotonic() < deadline:
            # Rescan: a member can fork between enumeration and its termination.
            if self.exited() is None or self.session_members(child.pid):
                self.signal_session(child.pid, signal.SIGKILL)
            self.reap_members(child.pid)
            if self.exited() is not None and not self.session_members(child.pid):
                break
            time.sleep(0.02)
        else:
            # Keep ownership and the unreaped leader; no subsequent start allowed.
            raise UpdateError()
        code = child.wait(timeout=self.kill_seconds)
        self.process = None
        if not graceful or code != 0:
            raise UpdateError()

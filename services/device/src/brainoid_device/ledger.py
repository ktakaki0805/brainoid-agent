"""Device-only durable metadata. No payload, audio, conversation or secret values.

This ledger is a prerequisite for DEV-07 authorization/execution, not an HA adapter.
Every receipt is returned after COMMIT. An I/O error quarantines this instance until
reopened: an ambiguous commit must be reconciled from disk, never blindly retried.
"""

import fcntl
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from brainoid_device.filesystem import open_directory
from brainoid_device.migrations import migrate, validate_format
from brainoid_device.storage import CredentialStore

DB_NAME = "agent.sqlite3"
INITIALIZED_MARKER = ".initialized"
RETENTION_SECONDS = 7 * 24 * 60 * 60
CommandStatus = Literal["accepted", "started", "succeeded", "failed", "expired", "outcome_unknown"]
TerminalStatus = Literal["succeeded", "failed", "expired", "outcome_unknown"]
Purpose = Literal["cloud_refresh", "home_assistant"]


class StorageUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("device_storage_recovery_required")


class LedgerConflict(ValueError):
    def __init__(self) -> None:
        super().__init__("device_ledger_conflict")


def _uuid(value: str) -> None:
    if str(UUID(value)) != value:
        raise ValueError("canonical UUID required")


def _integer(value: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < 2**62:
        raise ValueError("invalid bounded integer")


@dataclass(frozen=True)
class Preferences:
    protocol_version: int = 1
    retention_seconds: int = RETENTION_SECONDS
    volume_pct: int = 50
    microphone_muted: bool = False

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            raise ValueError("unsupported protocol version")
        _integer(self.retention_seconds, RETENTION_SECONDS)
        if self.retention_seconds >= 2**62 // 1000:
            raise ValueError("invalid retention interval")
        _integer(self.volume_pct)
        if self.volume_pct > 100 or type(self.microphone_muted) is not bool:
            raise ValueError("invalid device preferences")


@dataclass(frozen=True)
class Registration:
    tenant_id: str
    device_id: str
    epoch: str
    connection_generation: int
    delivery_cursor: int


@dataclass(frozen=True)
class CommandDelivery:
    tenant_id: str
    device_id: str
    epoch: str
    connection_generation: int
    command_id: str
    delivery_seq: int
    payload_digest: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.device_id, self.epoch, self.command_id):
            _uuid(value)
        for number in (self.connection_generation, self.delivery_seq):
            _integer(number, 1)
        _integer(self.expires_at_ms)
        if re.fullmatch("[0-9a-f]{64}", self.payload_digest) is None:
            raise ValueError("SHA-256 digest required")


@dataclass(frozen=True)
class StoredCommand:
    command_id: str
    execution_id: str
    epoch: str
    payload_digest: str
    delivery_seq: int
    received_generation: int
    start_generation: int | None
    status: CommandStatus
    accepted_at_ms: int
    expires_at_ms: int
    updated_at_ms: int
    result_acked: bool

    @property
    def wire_status(self) -> str:
        return "outcome_unknown" if self.status == "started" else self.status


@dataclass(frozen=True)
class StorageSnapshot:
    status: Literal["unregistered", "ready", "reset_pending", "recovery_required"]
    registration: Registration | None = None
    preferences: Preferences | None = None

    @property
    def permits_operations(self) -> bool:
        # Storage prerequisite only. Cloud lease, grants, session, target and
        # current authorization still have to be checked by the adapters.
        return self.status == "ready"


def _registration(connection: sqlite3.Connection) -> Registration | None:
    row = connection.execute(
        "SELECT tenant_id,device_id,epoch,connection_generation,delivery_cursor FROM registration"
    ).fetchone()
    return Registration(*row) if row is not None else None


def _command(connection: sqlite3.Connection, command_id: str) -> StoredCommand | None:
    row = connection.execute(
        """SELECT command_id,execution_id,epoch,payload_digest,delivery_seq,received_generation,
        start_generation,status,accepted_at_ms,expires_at_ms,updated_at_ms,result_acked
        FROM commands WHERE command_id=?""",
        (command_id,),
    ).fetchone()
    return StoredCommand(*row) if row is not None else None


class DeviceStorage:
    def __init__(
        self, path: Path, credentials: CredentialStore, *, cleanup_private: Callable[[], None]
    ) -> None:
        self._credentials = credentials
        self._cleanup_private = cleanup_private
        self._lock = threading.RLock()
        self._fd = -1
        self._db: sqlite3.Connection | None = None
        self._failed = False
        self._resetting = False
        self._owns_state = False
        self._db_identity: tuple[int, int] | None = None
        try:
            self._fd = open_directory(path)
            self._check_directory()
            # Exactly one daemon owns a state directory, across threads/processes.
            # Lock the stable directory inode, never a reset-deleted file.
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._owns_state = True
            self._open_database()
        except (OSError, ValueError, sqlite3.Error, StorageUnavailable):
            self._failed = True
        # Even with corrupt/unsupported SQLite, a pending reset erases credentials
        # and private temporary data, but cannot remove the fence or report success.
        try:
            if self._credentials.reset_pending:
                self.local_reset()
        except (OSError, RuntimeError, ValueError):
            self._failed = True

    def _check_directory(self) -> None:
        info = os.fstat(self._fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise OSError("unsafe state directory")

    def _check_files(self) -> None:
        self._check_directory()
        names = os.listdir(self._fd)
        if self._db_identity is not None and not {DB_NAME, INITIALIZED_MARKER}.issubset(names):
            raise OSError("initialized storage missing")
        for name in names:
            if name not in {DB_NAME, DB_NAME + "-journal", INITIALIZED_MARKER}:
                raise OSError("unexpected state file")
            info = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise OSError("unsafe state file")
            if name == DB_NAME and self._db_identity is not None:
                if (info.st_dev, info.st_ino) != self._db_identity or info.st_size == 0:
                    raise OSError("database replaced or truncated")

    def _open_database(self) -> None:
        self._check_files()
        initialized = INITIALIZED_MARKER in os.listdir(self._fd)
        if initialized:
            # Missing or empty used storage is data loss, never a fresh install.
            if os.stat(DB_NAME, dir_fd=self._fd, follow_symlinks=False).st_size == 0:
                raise sqlite3.DatabaseError("initialized database lost")
        try:
            fd = os.open(
                DB_NAME,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fd,
            )
        except FileExistsError:
            pass
        else:
            os.close(fd)
            os.fsync(self._fd)
        self._db = sqlite3.connect(
            f"/proc/self/fd/{self._fd}/{DB_NAME}",
            timeout=0.2,
            isolation_level=None,
            check_same_thread=False,
        )
        db = self._db
        # Refuse newer formats before applying any persistent PRAGMA or migration.
        validate_format(db)
        for setting, expected in (
            ("journal_mode=DELETE", "delete"),
            ("synchronous=EXTRA", 3),
            ("foreign_keys=ON", 1),
            ("secure_delete=ON", 1),
            ("trusted_schema=OFF", 0),
        ):
            db.execute(f"PRAGMA {setting}")
            if db.execute(f"PRAGMA {setting.split('=')[0]}").fetchone()[0] != expected:
                raise sqlite3.DatabaseError("unsupported SQLite settings")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("storage integrity failure")
        migrate(db)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.DatabaseError("storage references failure")
        with self._transaction(allow_reset=True):
            pass
        info = os.stat(DB_NAME, dir_fd=self._fd, follow_symlinks=False)
        self._db_identity = (info.st_dev, info.st_ino)
        if not initialized:
            fd = os.open(
                INITIALIZED_MARKER,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._fd,
            )
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self._fd)

    @contextmanager
    def _transaction(self, *, allow_reset: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = self._db
            if self._failed or db is None:
                raise StorageUnavailable()
            try:
                if not allow_reset and (self._resetting or self._credentials.reset_pending):
                    raise StorageUnavailable()
                self._check_files()
                db.execute("BEGIN IMMEDIATE")
                # A real write detects read-only/full storage even on health/read.
                if db.execute("UPDATE settings SET revision=revision+1 WHERE id=1").rowcount != 1:
                    raise sqlite3.DatabaseError("missing settings")
                yield db
                db.execute("COMMIT")
            except BaseException as exc:
                if isinstance(exc, (OSError, sqlite3.Error)):
                    self._failed = True
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except sqlite3.Error:
                        self._failed = True
                if isinstance(exc, (OSError, sqlite3.Error)):
                    raise StorageUnavailable() from None
                raise

    def snapshot(self) -> StorageSnapshot:
        with self._lock:
            try:
                if self._resetting or self._credentials.reset_pending:
                    return StorageSnapshot("reset_pending")
                with self._transaction() as db:
                    registration = _registration(db)
                    row = db.execute("""SELECT protocol_version,retention_seconds,volume_pct,
                        microphone_muted FROM settings WHERE id=1""").fetchone()
                    preferences = Preferences(row[0], row[1], row[2], bool(row[3]))
                return StorageSnapshot(
                    "ready" if registration else "unregistered", registration, preferences
                )
            except (OSError, RuntimeError, ValueError):
                self._failed = True
                return StorageSnapshot("recovery_required")

    def save_preferences(self, preferences: Preferences) -> None:
        with self._transaction() as db:
            db.execute(
                """UPDATE settings SET protocol_version=?,retention_seconds=?,volume_pct=?,
                microphone_muted=? WHERE id=1""",
                (
                    preferences.protocol_version,
                    preferences.retention_seconds,
                    preferences.volume_pct,
                    int(preferences.microphone_muted),
                ),
            )

    def register(self, tenant_id: str, device_id: str) -> Registration:
        _uuid(tenant_id)
        _uuid(device_id)
        with self._transaction() as db:
            existing = _registration(db)
            if existing is not None:
                if (existing.tenant_id, existing.device_id) != (tenant_id, device_id):
                    raise LedgerConflict()
                return existing
            registration = Registration(tenant_id, device_id, str(uuid4()), 0, 0)
            db.execute(
                "INSERT INTO registration VALUES (1,?,?,?,?,?)",
                (tenant_id, device_id, registration.epoch, 0, 0),
            )
        return registration

    @staticmethod
    def _bound(db: sqlite3.Connection, epoch: str, generation: int | None = None) -> Registration:
        binding = _registration(db)
        if (
            binding is None
            or binding.epoch != epoch
            or (
                generation is not None
                and (generation < 1 or binding.connection_generation != generation)
            )
        ):
            raise LedgerConflict()
        return binding

    def set_generation(self, epoch: str, generation: int) -> None:
        _integer(generation, 1)
        with self._transaction() as db:
            binding = self._bound(db, epoch)
            if generation < binding.connection_generation:
                raise LedgerConflict()
            db.execute("UPDATE registration SET connection_generation=?", (generation,))

    def save_credential_reference(self, epoch: str, purpose: Purpose, name: str) -> None:
        if purpose not in {"cloud_refresh", "home_assistant"}:
            raise ValueError("invalid credential purpose")
        CredentialStore._name(name)
        with self._transaction() as db:
            self._bound(db, epoch)
            db.execute(
                "INSERT INTO credential_refs VALUES (?,?) ON CONFLICT(purpose) "
                "DO UPDATE SET name=excluded.name",
                (purpose, name),
            )

    def credential_reference(self, epoch: str, purpose: Purpose) -> str | None:
        with self._transaction() as db:
            self._bound(db, epoch)
            row = db.execute(
                "SELECT name FROM credential_refs WHERE purpose=?", (purpose,)
            ).fetchone()
        return str(row[0]) if row else None

    def receive_command(
        self, delivery: CommandDelivery, *, expected_cursor: int, now_ms: int, clock_trusted: bool
    ) -> StoredCommand:
        _integer(now_ms)
        _integer(expected_cursor)
        if clock_trusted is not True:
            raise LedgerConflict()
        with self._transaction() as db:
            binding = self._bound(db, delivery.epoch, delivery.connection_generation)
            if (binding.tenant_id, binding.device_id) != (delivery.tenant_id, delivery.device_id):
                raise LedgerConflict()
            saved = _command(db, delivery.command_id)
            if saved is not None:
                if (saved.epoch, saved.payload_digest, saved.delivery_seq, saved.expires_at_ms) != (
                    delivery.epoch,
                    delivery.payload_digest,
                    delivery.delivery_seq,
                    delivery.expires_at_ms,
                ):
                    raise LedgerConflict()
                return saved
            if (
                binding.delivery_cursor != expected_cursor
                or delivery.delivery_seq <= expected_cursor
            ):
                raise LedgerConflict()
            status = "accepted" if delivery.expires_at_ms > now_ms else "expired"
            db.execute(
                """INSERT INTO commands (command_id,execution_id,epoch,payload_digest,
                delivery_seq,received_generation,status,accepted_at_ms,expires_at_ms,updated_at_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    delivery.command_id,
                    str(uuid4()),
                    delivery.epoch,
                    delivery.payload_digest,
                    delivery.delivery_seq,
                    delivery.connection_generation,
                    status,
                    now_ms,
                    delivery.expires_at_ms,
                    now_ms,
                ),
            )
            db.execute("UPDATE registration SET delivery_cursor=?", (delivery.delivery_seq,))
            result = _command(db, delivery.command_id)
            assert result is not None
        return result

    def advance_cursor(self, epoch: str, generation: int, *, expected: int, cursor: int) -> None:
        """For ordered non-command metadata only; never skip an unrecorded command."""
        _integer(cursor)
        with self._transaction() as db:
            binding = self._bound(db, epoch, generation)
            if expected != binding.delivery_cursor or cursor < expected:
                raise LedgerConflict()
            db.execute("UPDATE registration SET delivery_cursor=?", (cursor,))

    def command(self, epoch: str, command_id: str) -> StoredCommand | None:
        with self._transaction() as db:
            self._bound(db, epoch)
            return _command(db, command_id)

    def record_started(
        self,
        epoch: str,
        command_id: str,
        execution_id: str,
        generation: int,
        *,
        grant_expires_at_ms: int,
        now_ms: int,
        clock_trusted: bool,
    ) -> bool:
        """Consume a validated grant once. False is never permission to call HA."""
        _integer(now_ms)
        _integer(grant_expires_at_ms)
        if clock_trusted is not True:
            raise LedgerConflict()
        with self._transaction() as db:
            self._bound(db, epoch, generation)
            saved = _command(db, command_id)
            if saved is None or saved.execution_id != execution_id:
                raise LedgerConflict()
            if saved.status != "accepted":
                return False
            if (
                min(saved.expires_at_ms, grant_expires_at_ms) <= now_ms
                or now_ms < saved.updated_at_ms
            ):
                raise LedgerConflict()
            db.execute(
                "UPDATE commands SET status='started',start_generation=?,updated_at_ms=? "
                "WHERE command_id=?",
                (generation, now_ms, command_id),
            )
        return True

    def record_result(
        self, epoch: str, command_id: str, execution_id: str, *, status: TerminalStatus, now_ms: int
    ) -> None:
        _integer(now_ms)
        transitions = {
            "accepted": {"expired", "failed"},
            "started": {"succeeded", "failed", "outcome_unknown"},
            "outcome_unknown": {"succeeded", "failed"},
        }
        with self._transaction() as db:
            self._bound(db, epoch)
            saved = _command(db, command_id)
            if saved is None or saved.execution_id != execution_id:
                raise LedgerConflict()
            if saved.status == status:
                return
            if status not in transitions.get(saved.status, set()) or now_ms < saved.updated_at_ms:
                raise LedgerConflict()
            db.execute(
                "UPDATE commands SET status=?,updated_at_ms=?,result_acked=0 WHERE command_id=?",
                (status, now_ms, command_id),
            )

    def acknowledge_result(
        self, epoch: str, command_id: str, execution_id: str, status: TerminalStatus
    ) -> None:
        with self._transaction() as db:
            self._bound(db, epoch)
            saved = _command(db, command_id)
            if saved is None or saved.execution_id != execution_id or saved.status != status:
                raise LedgerConflict()
            if status not in {"succeeded", "failed", "expired", "outcome_unknown"}:
                raise LedgerConflict()
            db.execute("UPDATE commands SET result_acked=1 WHERE command_id=?", (command_id,))

    def prune_commands(self, *, now_ms: int, clock_trusted: bool) -> int:
        _integer(now_ms)
        if clock_trusted is not True:
            raise LedgerConflict()
        with self._transaction() as db:
            retention = db.execute("SELECT retention_seconds FROM settings").fetchone()[0]
            deleted = db.execute(
                """DELETE FROM commands WHERE status IN ('succeeded','failed','expired')
                AND result_acked=1 AND expires_at_ms<=? AND updated_at_ms<=?""",
                (now_ms, now_ms - retention * 1000),
            ).rowcount
        return deleted

    def local_reset(self) -> bool:
        """Resumable intent -> secrets -> private cleanup -> SQL -> intent removal.

        Returns False with the fence retained on *any* incomplete step. Corrupt or
        future-version databases require operator recovery, never silent replacement.
        """
        with self._lock:
            if not self._owns_state:
                return False
            self._resetting = True
            try:
                self._credentials.begin_reset()
                self._credentials.erase_for_reset()
                self._cleanup_private()
                with self._transaction(allow_reset=True) as db:
                    db.execute("DELETE FROM commands")
                    db.execute("DELETE FROM credential_refs")
                    db.execute("DELETE FROM registration")
                    db.execute(
                        "UPDATE settings SET protocol_version=1,retention_seconds=?,"
                        "volume_pct=50,microphone_muted=0",
                        (RETENTION_SECONDS,),
                    )
                self._credentials.finish_reset()
                self._resetting = False
                return True
            except Exception:
                # Do not leak paths, credential names, exception values or data.
                # Filesystem/cleanup failures may be retried; SQL failure is latched.
                return False

    def close(self) -> None:
        with self._lock:
            self._failed = True
            self._owns_state = False
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1

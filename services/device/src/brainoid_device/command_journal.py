"""Schema-2 compatible execution extension, committed with the DEV02 ledger.

The versioned tables add no columns/triggers to the updater's base ABI. Both
foreign keys cascade so old daemons' reset/pruning also erase this extension.
Only fixed command metadata and bounded protocol reports are retained.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

from brainoid_device.cloud_wire import WS_VALIDATOR
from brainoid_device.ledger import (
    CommandDelivery,
    DeviceStorage,
    LedgerConflict,
    StoredCommand,
    TerminalStatus,
    _command,
)

MAX_PENDING = 256


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Execution:
    command: StoredCommand
    delivery: dict[str, Any]
    reports: dict[str, dict[str, Any]]
    ha_accepted: bool
    settled: bool


class CommandJournal:
    def __init__(self, storage: DeviceStorage) -> None:
        self.storage = storage

    def prepare(self, epoch: str) -> None:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            extensions = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND "
                    "(name LIKE 'command_execution_v%' OR name LIKE 'command_clock_v%')"
                )
            }
            if extensions - {"command_execution_v1", "command_clock_v1"}:
                raise LedgerConflict()
            db.execute("""CREATE TABLE IF NOT EXISTS command_execution_v1 (
                command_id TEXT PRIMARY KEY REFERENCES commands(command_id) ON DELETE CASCADE,
                delivery TEXT NOT NULL CHECK(length(delivery)<=8192),
                reports TEXT NOT NULL CHECK(length(reports)<=8192),
                ha_accepted INTEGER NOT NULL DEFAULT 0 CHECK(ha_accepted IN (0,1)),
                settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0,1))
            ) STRICT""")
            db.execute("""CREATE TABLE IF NOT EXISTS command_clock_v1 (
                epoch TEXT PRIMARY KEY REFERENCES registration(epoch) ON DELETE CASCADE,
                wall_ms INTEGER NOT NULL
            ) STRICT""")

    def clock(self, epoch: str, wall_ms: int) -> None:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            row = db.execute(
                "SELECT wall_ms FROM command_clock_v1 WHERE epoch=?", (epoch,)
            ).fetchone()
            if row is not None and wall_ms < row[0]:
                raise LedgerConflict()
            db.execute(
                "INSERT INTO command_clock_v1 VALUES (?,?) ON CONFLICT(epoch) DO UPDATE SET wall_ms=excluded.wall_ms",
                (epoch, wall_ms),
            )

    def receive(
        self, epoch: str, generation: int, payload: dict[str, Any], *, now_ms: int, observed_at: str
    ) -> Execution:
        from datetime import datetime

        with self.storage._transaction() as db:
            registration = self.storage._bound(db, epoch, generation)
            saved = _command(db, payload["command_id"])
            if (
                saved is None
                and db.execute(
                    "SELECT count(*) FROM commands JOIN command_execution_v1 USING(command_id) WHERE result_acked=0 AND settled=0"
                ).fetchone()[0]
                >= MAX_PENDING
            ):
                raise LedgerConflict()
            saved = self.storage._receive(
                db,
                CommandDelivery(
                    payload["tenant_id"],
                    payload["device_id"],
                    epoch,
                    generation,
                    payload["command_id"],
                    payload["delivery_seq"],
                    digest(payload),
                    int(datetime.fromisoformat(payload["expires_at"]).timestamp() * 1000),
                ),
                expected_cursor=registration.delivery_cursor,
                now_ms=now_ms,
                reordered=True,
            )
            row = db.execute(
                "SELECT delivery FROM command_execution_v1 WHERE command_id=?", (saved.command_id,)
            ).fetchone()
            if row is None:
                # A base-only old receipt may be adopted only before execution.
                if saved.status not in {"accepted", "expired"}:
                    raise LedgerConflict()
                report = {
                    "command_id": saved.command_id,
                    "execution_id": saved.execution_id,
                    "status": saved.status,
                    "observed_at": observed_at,
                }
                db.execute(
                    "INSERT INTO command_execution_v1 VALUES (?,?,?,0,0)",
                    (saved.command_id, canonical(payload), canonical({saved.status: report})),
                )
            elif row[0] != canonical(payload):
                raise LedgerConflict()
        return self.get(epoch, saved.command_id)

    def get(self, epoch: str, identity: str) -> Execution:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            saved = _command(db, identity)
            row = db.execute(
                "SELECT delivery,reports,ha_accepted,settled FROM command_execution_v1 WHERE command_id=?",
                (identity,),
            ).fetchone()
            if saved is None or row is None:
                raise LedgerConflict()
            return Execution(
                saved, json.loads(row[0]), json.loads(row[1]), bool(row[2]), bool(row[3])
            )

    def pending(self, epoch: str) -> list[str]:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            return [
                row[0]
                for row in db.execute(
                    "SELECT commands.command_id FROM commands JOIN command_execution_v1 USING(command_id) "
                    "WHERE result_acked=0 AND settled=0 ORDER BY delivery_seq LIMIT ?",
                    (MAX_PENDING,),
                )
            ]

    def cancelled(self, epoch: str, identity: str, *, now_ms: int) -> None:
        """An authorized canonical cancel retires pending work, never fabricates an ACK."""
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            saved = _command(db, identity)
            if saved is None or saved.status != "accepted" or now_ms < saved.updated_at_ms:
                raise LedgerConflict()
            db.execute("UPDATE command_execution_v1 SET settled=1 WHERE command_id=?", (identity,))
            db.execute("UPDATE commands SET updated_at_ms=? WHERE command_id=?", (now_ms, identity))

    def prune(self, epoch: str, *, now_ms: int) -> None:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            retention = db.execute("SELECT retention_seconds FROM settings").fetchone()[0]
            db.execute(
                "DELETE FROM commands WHERE command_id IN "
                "(SELECT command_id FROM command_execution_v1 WHERE settled=1) "
                "AND expires_at_ms<=? AND updated_at_ms<=?",
                (now_ms, now_ms - retention * 1000),
            )

    def accepted(self, epoch: str, identity: str) -> None:
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            saved = _command(db, identity)
            if saved is None or saved.status != "started":
                raise LedgerConflict()
            db.execute(
                "UPDATE command_execution_v1 SET ha_accepted=1 WHERE command_id=?", (identity,)
            )

    def finish(self, epoch: str, report: dict[str, Any], *, now_ms: int) -> None:
        # Validate the same installed schema as the transport; reports never
        # contain provider response bodies, exception strings or arbitrary keys.
        if not WS_VALIDATOR.evolve(
            schema=WS_VALIDATOR.schema["$defs"]["command_status"]["properties"]["payload"]
        ).is_valid(report):
            raise LedgerConflict()
        with self.storage._transaction() as db:
            self.storage._bound(db, epoch)
            saved = _command(db, report["command_id"])
            row = db.execute(
                "SELECT reports FROM command_execution_v1 WHERE command_id=?",
                (report["command_id"],),
            ).fetchone()
            if saved is None or row is None or saved.execution_id != report["execution_id"]:
                raise LedgerConflict()
            reports = json.loads(row[0])
            status = report["status"]
            if status in reports:
                if reports[status] != report or saved.status != status:
                    raise LedgerConflict()
                return
            transitions = {
                "accepted": {"expired", "failed"},
                "started": {"succeeded", "failed", "outcome_unknown"},
                "outcome_unknown": {"succeeded", "failed"},
            }
            if status not in transitions.get(saved.status, set()) or now_ms < saved.updated_at_ms:
                raise LedgerConflict()
            reports[status] = report
            db.execute(
                "UPDATE command_execution_v1 SET reports=? WHERE command_id=?",
                (canonical(reports), saved.command_id),
            )
            db.execute(
                "UPDATE commands SET status=?,updated_at_ms=?,result_acked=0 WHERE command_id=?",
                (status, now_ms, saved.command_id),
            )

    def acknowledge(self, epoch: str, value: Execution) -> None:
        self.storage.acknowledge_result(
            epoch,
            value.command.command_id,
            value.command.execution_id,
            cast(TerminalStatus, value.command.status),
        )

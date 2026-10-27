"""Explicit local encrypted SecretStore for fake/emulator composition only.

One SQLite transaction serializes immutable versions and persistent scope/slot
revocation. This is not Google Secret Manager and is never a runtime fallback.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.deletion_ledger import DeletionIntent
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import TypeAdapter


class LocalCalendarSecrets:
    def __init__(self, directory: Path, key: bytes, *, now: Callable[[], datetime]) -> None:
        if len(key) != 32:
            raise ValueError("Explicit 256-bit secret encryption key required")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            raise ValueError("Secret directory must be private")
        self.path, self.cipher, self.now = directory / "calendar.sqlite", AESGCM(key), now
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS fences (scope TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS member_fences (tenant TEXT, subject TEXT, cutoff INTEGER, PRIMARY KEY(tenant,subject))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS versions (slot TEXT PRIMARY KEY, tenant TEXT, "
                "subject TEXT, binding TEXT, expiry REAL, sealed BLOB, generation INTEGER)"
            )

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise SecretUnavailable() from None
        finally:
            db.close()

    @staticmethod
    def fences(slot: str, tenant: str, subject: str) -> tuple[str, ...]:
        return (
            "slot:" + slot,
            "tenant:" + tenant,
            "account:" + subject,
        )

    def blocked(
        self, db: sqlite3.Connection, slot: str, tenant: str, subject: str, generation: int
    ) -> bool:
        cutoff = db.execute(
            "SELECT cutoff FROM member_fences WHERE tenant=? AND subject=?", (tenant, subject)
        ).fetchone()
        return (
            generation < 1
            or (cutoff is not None and generation <= cutoff[0])
            or any(
                db.execute("SELECT 1 FROM fences WHERE scope=?", (f,)).fetchone()
                for f in self.fences(slot, tenant, subject)
            )
        )

    def put(
        self,
        slot: ResourceId,
        *,
        tenant: ResourceId,
        user: FirebaseUid,
        binding: str,
        content: bytes,
        expires_at: datetime | None,
        generation: int = 1,
    ) -> str:
        TypeAdapter(ResourceId).validate_python(slot)
        subject = subject_id(user)
        expiry = None if expires_at is None else expires_at.timestamp()
        with self.db() as db:
            if expiry is not None and self.now().timestamp() >= expiry:
                raise SecretUnavailable()
            if self.blocked(db, slot, tenant, subject, generation):
                raise SecretUnavailable()
            prior = db.execute(
                "SELECT tenant,subject,binding,expiry,generation FROM versions WHERE slot=?",
                (slot,),
            ).fetchone()
            if prior is not None:
                if prior != (tenant, subject, binding, expiry, generation):
                    raise SecretUnavailable()
            else:
                nonce = os.urandom(12)
                sealed = nonce + self.cipher.encrypt(nonce, content, slot.encode())
                db.execute(
                    "INSERT INTO versions VALUES(?,?,?,?,?,?,?)",
                    (slot, tenant, subject, binding, expiry, sealed, generation),
                )
        return f"local-calendar/{slot}/versions/1"

    def get(self, reference: str) -> bytes:
        parts = reference.split("/")
        if len(parts) != 4 or parts[0] != "local-calendar" or parts[2:] != ["versions", "1"]:
            raise SecretUnavailable()
        slot = parts[1]
        with self.db() as db:
            row = db.execute("SELECT expiry,sealed FROM versions WHERE slot=?", (slot,)).fetchone()
            if row is None or (row[0] is not None and self.now().timestamp() >= row[0]):
                raise SecretUnavailable()
            return self.cipher.decrypt(row[1][:12], row[1][12:], slot.encode())

    def revoke(self, slot: ResourceId) -> None:
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO fences VALUES(?)", ("slot:" + slot,))
            db.execute("DELETE FROM versions WHERE slot=?", (slot,))

    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        with self.db() as db:
            args: tuple[str | int, ...]
            for scope in intent.scopes:
                if scope.kind == "integration":
                    for slot in scope.secret_slots:
                        db.execute("INSERT OR IGNORE INTO fences VALUES(?)", ("slot:" + slot,))
                        db.execute("DELETE FROM versions WHERE slot=?", (slot,))
                    continue
                if scope.kind == "tenant":
                    fence, clause, args = (
                        "tenant:" + str(scope.tenant_id),
                        "tenant=?",
                        (str(scope.tenant_id),),
                    )
                elif scope.kind == "account":
                    fence, clause, args = (
                        "account:" + scope.resource_id,
                        "subject=?",
                        (scope.resource_id,),
                    )
                else:
                    fence = f"member:{scope.tenant_id}:{scope.resource_id}"
                    clause, args = (
                        "tenant=? AND subject=? AND generation<=?",
                        (str(scope.tenant_id), scope.resource_id, scope.version + 1),
                    )
                if scope.kind == "member":
                    db.execute(
                        "INSERT INTO member_fences VALUES(?,?,?) ON CONFLICT(tenant,subject) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff)",
                        args,
                    )
                db.execute("INSERT OR IGNORE INTO fences VALUES(?)", (fence,))
                db.execute("DELETE FROM versions WHERE " + clause, args)
        return "calendar-secrets-revoked-v1"

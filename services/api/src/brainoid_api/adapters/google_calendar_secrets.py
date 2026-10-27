"""Secret Manager numeric-version adapter with an explicit durable fencing registry.

The registry contains scope, binding and effect state only; credentials are sent
only to the injected Secret Manager transport. No SDK credentials or cloud calls
are created by default. A shared registry is required for every adapter instance.
"""

import base64
import json
from datetime import datetime
from typing import Protocol

from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_memory.calendar_oauth import SecretUnavailable
from brainoid_memory.deletion_ledger import DeletionIntent

from brainoid_api.adapters.calendar_secrets import LocalCalendarSecrets


class SecretManagerTransport(Protocol):
    """Explicit authenticated Google transport; exceptions must not be logged.

    create uses the caller's fixed secret ID (AlreadyExists is success).
    add returns the exact numeric version resource. access requires that version.
    destroy treats an already destroyed version as success; missing is NOT proof
    that an uncertain add cannot still arrive. Each request has a bounded timeout.
    """

    def create(self, name: str) -> None: ...
    def add(self, name: str, payload: bytes) -> str: ...
    def access(self, reference: str) -> bytes: ...
    def destroy(self, reference: str) -> None: ...


class GoogleCalendarSecrets:
    def __init__(
        self, registry: LocalCalendarSecrets, transport: SecretManagerTransport, *, project: str
    ) -> None:
        if not project or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in project):
            raise ValueError("Explicit Secret Manager project required")
        self.registry, self.transport, self.project = registry, transport, project
        with registry.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS remote_versions (slot TEXT PRIMARY KEY, tenant TEXT, subject TEXT, binding TEXT, expiry REAL, state TEXT, uncertain INTEGER, generation INTEGER)"
            )

    def reference(self, slot: str) -> str:
        return f"projects/{self.project}/secrets/calendar-{slot}/versions/1"

    def fenced(self, slot: str, tenant: str, subject: str) -> bool:
        with self.registry.db() as db:
            return any(
                db.execute("SELECT 1 FROM fences WHERE scope=?", (scope,)).fetchone()
                for scope in self.registry.fences(slot, tenant, subject)
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
        subject, expiry = subject_id(user), None if expires_at is None else expires_at.timestamp()
        with self.registry.db() as db:
            if (
                expiry is not None and self.registry.now().timestamp() >= expiry
            ) or self.registry.blocked(db, slot, tenant, subject, generation):
                raise SecretUnavailable()
            row = db.execute(
                "SELECT tenant,subject,binding,expiry,state,generation FROM remote_versions WHERE slot=?",
                (slot,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO remote_versions VALUES(?,?,?,?,?,'reserved',0,?)",
                    (slot, tenant, subject, binding, expiry, generation),
                )
            elif row[:4] != (tenant, subject, binding, expiry) or (
                row[4] == "revoked" or row[5] != generation
            ):
                raise SecretUnavailable()
        reference = self.reference(slot)
        try:
            # Secret creation is named and idempotent; version addition is not.
            self.transport.create(reference.rsplit("/versions/", 1)[0])
            with self.registry.db() as db:
                row = db.execute(
                    "SELECT state FROM remote_versions WHERE slot=?", (slot,)
                ).fetchone()
                add = row == ("reserved",)
                if add:
                    db.execute(
                        "UPDATE remote_versions SET state='adding',uncertain=1 WHERE slot=?",
                        (slot,),
                    )
            if add:
                payload = json.dumps(
                    {"binding": binding, "content": base64.b64encode(content).decode()}
                ).encode()
                returned = self.transport.add(reference.rsplit("/versions/", 1)[0], payload)
                if returned != reference:
                    raise SecretUnavailable()
            # Exact-version access reconciles add response loss without a second add.
            saved = json.loads(self.transport.access(reference))
            if saved["binding"] != binding:
                raise SecretUnavailable()
            with self.registry.db() as db:
                row = db.execute(
                    "SELECT state FROM remote_versions WHERE slot=?", (slot,)
                ).fetchone()
                revoked = row == ("revoked",)
                if not revoked:
                    db.execute(
                        "UPDATE remote_versions SET state='ready',uncertain=0 WHERE slot=?", (slot,)
                    )
            if revoked or self.fenced(slot, tenant, subject):
                self.revoke(slot)
                raise SecretUnavailable()
            return reference
        except Exception:
            raise SecretUnavailable() from None

    def get(self, reference: str) -> bytes:
        prefix = f"projects/{self.project}/secrets/calendar-"
        if not reference.startswith(prefix) or not reference.endswith("/versions/1"):
            raise SecretUnavailable()
        slot = reference[len(prefix) : -len("/versions/1")]
        try:
            with self.registry.db() as db:
                row = db.execute(
                    "SELECT tenant,subject,expiry,state,binding FROM remote_versions WHERE slot=?",
                    (slot,),
                ).fetchone()
            if (
                row is None
                or row[3] != "ready"
                or (row[2] is not None and self.registry.now().timestamp() >= row[2])
                or self.fenced(slot, row[0], row[1])
            ):
                raise SecretUnavailable()
            value = json.loads(self.transport.access(reference))
            if value["binding"] != row[4] or self.fenced(slot, row[0], row[1]):
                raise SecretUnavailable()
            return base64.b64decode(value["content"], validate=True)
        except Exception:
            raise SecretUnavailable() from None

    def revoke(self, slot: ResourceId) -> None:
        with self.registry.db() as db:
            db.execute("INSERT OR IGNORE INTO fences VALUES(?)", ("slot:" + slot,))
            row = db.execute(
                "SELECT state,uncertain FROM remote_versions WHERE slot=?", (slot,)
            ).fetchone()
            if row is None:
                return
            db.execute(
                "UPDATE remote_versions SET uncertain=CASE WHEN state='reserved' THEN -1 ELSE uncertain END,state='revoked' WHERE slot=?",
                (slot,),
            )
        try:
            if row[0] == "reserved" or row[1] == -1:
                # No add claim can now be acquired; empty receipt was committed.
                return
            # destroy must inspect metadata: already destroyed is success, while
            # missing is not a receipt for an uncertain add that may still arrive.
            self.transport.destroy(self.reference(slot))
            with self.registry.db() as db:
                db.execute("UPDATE remote_versions SET uncertain=0 WHERE slot=?", (slot,))
        except Exception:
            raise SecretUnavailable() from None

    def invalidate(self, intent: DeletionIntent, *, key: str) -> str:
        slots: set[str] = set()
        with self.registry.db() as db:
            args: tuple[str | int, ...]
            for scope in intent.scopes:
                if scope.kind == "integration":
                    for exact_slot in scope.secret_slots:
                        db.execute(
                            "INSERT OR IGNORE INTO fences VALUES(?)", ("slot:" + exact_slot,)
                        )
                        slots.add(exact_slot)
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
                    fence, clause, args = (
                        f"member:{scope.tenant_id}:{scope.resource_id}",
                        "tenant=? AND subject=? AND generation<=?",
                        (str(scope.tenant_id), scope.resource_id, scope.version + 1),
                    )
                if scope.kind == "member":
                    db.execute(
                        "INSERT INTO member_fences VALUES(?,?,?) ON CONFLICT(tenant,subject) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff)",
                        args,
                    )
                db.execute("INSERT OR IGNORE INTO fences VALUES(?)", (fence,))
                rows = db.execute(
                    "SELECT slot FROM remote_versions WHERE " + clause, args
                ).fetchall()
                for row in rows:
                    db.execute("INSERT OR IGNORE INTO fences VALUES(?)", ("slot:" + row[0],))
                    slots.add(row[0])
            # Reservations survive restarts, including revoked/uncertain effects.
            # Only this accepted scope and generation may be recovered here;
            # unrelated failed attempts retain their own independent cleanup.
        for slot in sorted(slots):
            self.revoke(ResourceId(slot))
        return "google-calendar-secrets-revoked-v1"

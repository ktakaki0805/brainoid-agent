"""Reminder producer inventory is removed by accepted member/account/tenant deletion."""

import copy
from uuid import uuid4

from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.memories import Memories
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.services.deletion_requests import DeleteTenant
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Transaction
from brainoid_memory.reminders import ReminderCreate, ReminderPatch, Reminders, reminder_path
from brainoid_memory.repository import FactTransaction
from test_deletion_requests import KEY, OWNER, USER, DeletionFixture


class ReminderLifecycleTests(DeletionFixture):
    def test_actual_reminder_effects_follow_each_lifecycle_scope(self) -> None:
        for kind in ("member", "account", "tenant"):
            with self.subTest(kind=kind):
                self.setUp()
                tenants = Tenants(
                    self.store,
                    Cursors(b"k" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                )
                memories = Memories(
                    self.store,
                    tenants,
                    Cursors(b"k" * 32, now=lambda: self.now),
                    ledger=self.authz,
                    now=lambda: self.now,
                    allow_untracked_bootstrap=True,
                )
                service = Reminders(self.store, now=lambda: self.now)
                preserved = {}
                own_id = None
                for actor, visibility in ((USER, "private"), (OWNER, "private"), (USER, "shared")):
                    body = ReminderCreate.model_validate(
                        {
                            "title": "REMINDER_PRIVATE_COPY",
                            "due_at": "2026-10-28T00:00:00Z",
                            "timezone": "UTC",
                            "delivery_target": {"kind": "user", "id": actor},
                            "visibility": visibility,
                            "display_policy": "never",
                        }
                    )
                    effect = ResourceId(str(uuid4()))

                    def opened(
                        tx: Transaction, actor: FirebaseUid = actor, memories: Memories = memories
                    ) -> FactTransaction:
                        return memories._open(tx, self.identity(actor), self.tenant, None)

                    record = service.mutate(
                        opened, body, "create-reminder-" + effect, effect=effect
                    )
                    service.mutate(
                        opened,
                        ReminderPatch(status="cancelled"),
                        "update-reminder-" + effect,
                        identity=record.id,
                        expected=1,
                        effect=ResourceId(str(uuid4())),
                    )
                    paths = [
                        p
                        for p, d in self.store.data.items()
                        if d.get("id") == record.id or d.get("record", {}).get("id") == record.id
                    ]
                    if actor == USER and visibility == "private":
                        own_id = record.id
                    elif kind != "tenant":
                        preserved.update({p: copy.deepcopy(self.store.data[p]) for p in paths})
                if kind == "tenant":
                    operation = self.repo.create(
                        self.identity(OWNER),
                        DeleteTenant(
                            kind="delete_tenant",
                            confirmation_name=self.store.data[f"tenants/{self.tenant}"]["name"],
                        ),
                        KEY,
                        self.tenant,
                    )
                else:
                    operation = self.accept(account=kind == "account")
                self.drain(operation, subject_id(USER) if kind == "account" else self.tenant)
                assert own_id is not None
                self.assertNotIn(reminder_path(self.tenant, own_id), self.store.data)
                self.assertFalse(
                    any(
                        d.get("id") == own_id or d.get("record", {}).get("id") == own_id
                        for d in self.store.data.values()
                    )
                )
                for p, document in preserved.items():
                    self.assertEqual(self.store.data[p], document)
